"""Structural (AST-pattern) search and rewrite, via `ast-grep`.

`search_text` (`searching.py`) matches lines; a lot of real questions are
shaped like code, not like text -- "every call to `foo` with exactly two
arguments", regardless of whitespace, argument names, or which of the 500
call sites wraps the line differently. A regex for that either over-matches
(`foo\\(.*,.*\\)` also matches `foo(bar(1, 2))` as one hit spanning the
wrong argument boundary) or under-matches (misses a call split across
lines). `ast-grep` (via its `ast-grep-py` bindings, same Rust engine as the
CLI) matches on the parsed structure instead, so a pattern like
`foo($A, $B)` means exactly "a call to foo with two arguments", however it
is formatted.

This reuses the same tree-sitter grammars the index's own parser adapters
are built on (`core/parser/*_adapter.py`), so `ast_search`/`ast_replace`
support exactly the same five languages the index does: cpp, java, php,
javascript, python -- no new language coverage to keep in sync separately.

This is deliberately a peer of `search_text`/`editing.py`, not a
`core/semantic/` backend: it answers structural questions from syntax
alone (what does this code look like), the same tier `core/semantic/backends.py`
draws the line at -- it never needs a type, a resolved call target, or a
project build, which is exactly why it works uniformly across all five
languages where the semantic backends only cover what each language's own
tool exposes.
"""

import re
from pathlib import Path
from typing import Any

from ast_grep_py import SgRoot

from code_intelligence.core.context.searching import _enclosing_symbol, _symbol_index
from code_intelligence.core.context.editing import StaleEditError
from code_intelligence.core.hashes.content_hash import content_hash
from code_intelligence.core.index.store import IndexStore

#: The five grammars `ast-grep-py` is asked to parse with -- exactly the
#: index's own supported languages (`core/parser/*_adapter.py`), so a
#: caller never gets a silent "not found" for a language the index does
#: index; unsupported files are skipped explicitly instead (see `_iter_target_files`).
SUPPORTED_LANGUAGES = frozenset({"cpp", "java", "php", "javascript", "python"})

#: Hard cap on returned matches, mirroring `searching.MAX_HITS`: a pattern
#: that matches ten thousand call sites is a pattern that needs narrowing.
MAX_MATCHES = 500

_SINGLE_VAR_RE = re.compile(r"(?<!\$)\$(?!\$)([A-Z_][A-Z0-9_]*)")
_MULTI_VAR_RE = re.compile(r"\$\$\$([A-Z_][A-Z0-9_]*)")


def _pattern_var_names(pattern: str) -> tuple[list[str], list[str]]:
    """The single-node (`$VAR`) and zero-or-more (`$$$VAR`) metavariable names in `pattern`.

    Extracted by regex rather than asking `ast-grep-py` for them: the
    bindings expose `get_match`/`get_multiple_matches` per-name but no
    "which names did this pattern declare" introspection, and the syntax
    itself (a `$` not adjacent to another `$`, vs. three in a row) is
    simple enough to recognize without round-tripping through the parser.
    """
    multi = _MULTI_VAR_RE.findall(pattern)
    # Drop the multi-var spans first so `$$$ARGS`'s middle `$`s are never
    # also read as a single-var `$$` (which `_SINGLE_VAR_RE`'s lookaround
    # already excludes) -- belt-and-suspenders against pattern text that
    # mixes both forms adjacently.
    single_source = _MULTI_VAR_RE.sub("", pattern)
    single = _SINGLE_VAR_RE.findall(single_source)
    return single, multi


def _captures(match: Any, single_vars: list[str], multi_vars: list[str]) -> dict[str, Any]:
    """The metavariables `pattern` declared, resolved against one actual `match`.

    A variable that did not capture (an optional branch the concrete match
    did not take) is simply absent from the result rather than reported as
    `null` -- so a caller iterating `captures.items()` never has to
    distinguish "captured nothing" from "was never a variable here".
    """
    captures: dict[str, Any] = {}
    for name in single_vars:
        node = match.get_match(name)
        if node is not None:
            captures[name] = node.text()
    for name in multi_vars:
        nodes = match.get_multiple_matches(name)
        if nodes:
            captures[name] = [node.text() for node in nodes]
    return captures


def _substitute(replacement: str, match: Any, single_vars: list[str], multi_vars: list[str]) -> str:
    """`replacement`, with `pattern`'s metavariables resolved against one actual `match`.

    `SgNode.replace()` treats its argument as literal text -- it does NOT
    substitute `$VAR` itself (confirmed against the installed
    `ast-grep-py`: replacing with a literal `"baz($A, $B)"` template writes
    the four characters `$A` verbatim, not the captured node's text). This
    is the substitution step ast-grep's own CLI does internally before
    handing a rewrite to `replace()`.

    Multi-vars are substituted first and fully (three `$` and the name
    become the joined text of every captured node, with no separator --
    the captured node list already includes each in-between comma/token
    verbatim, e.g. `$$$ARGS` over `1, 2, 3` captures `['1', ',', ' ', '2',
    ...]`), so no `$$$NAME` sequence remains when single-var substitution
    runs. Single vars are then substituted longest-name-first so `$ARGS2`
    can never be clobbered by a `$ARGS` replacement matching its prefix.
    A replacement function (not a plain string) is passed to `re.sub` so a
    captured value containing its own backslash-digit sequence is never
    misread as a backreference.
    """
    text = replacement
    for name in multi_vars:
        nodes = match.get_multiple_matches(name)
        joined = "".join(node.text() for node in nodes) if nodes else ""
        text = re.sub(r"\$\$\$" + re.escape(name) + r"\b", lambda _, v=joined: v, text)
    for name in sorted(single_vars, key=len, reverse=True):
        node = match.get_match(name)
        value = node.text() if node is not None else ""
        text = re.sub(
            r"(?<!\$)\$" + re.escape(name) + r"(?![A-Za-z0-9_])", lambda _, v=value: v, text
        )
    return text


def _iter_target_files(
    store: IndexStore,
    file: str | None,
    language: str | None,
    path_prefix: str | None,
    include_tests: bool,
) -> list[dict[str, Any]]:
    """Indexed file rows to run the pattern against, honoring the same filters `search_text` does."""
    if language is not None and language not in SUPPORTED_LANGUAGES:
        raise ValueError(
            f"language {language!r} is not one ast-grep is asked to parse here "
            f"(supported: {sorted(SUPPORTED_LANGUAGES)})"
        )
    rows = [file_row for file_row in store.list_files() if file_row["language"] in SUPPORTED_LANGUAGES]
    if file is not None:
        rows = [row for row in rows if row["path"] == file]
    if path_prefix:
        rows = [row for row in rows if row["path"].startswith(path_prefix)]
    if language:
        rows = [row for row in rows if row["language"] == language]
    if not include_tests:
        rows = [row for row in rows if not row["is_test_file"]]
    return rows


def ast_match_files(
    store: IndexStore,
    root: Path,
    pattern: str,
    language: str | None,
    path_prefix: str | None,
    include_tests: bool,
    max_files: int | None = None,
) -> dict[str, Any]:
    """Which indexed files a pattern matches in, and how many times — no match text.

    The discovery half of `ast_transform`: it returns per-file match counts
    (not the matched source), so a workspace-wide sweep can be planned and
    reported without transporting hundreds of snippets. `files` is ordered
    by descending match count and capped at `max_files` when given.
    """
    rows = _iter_target_files(store, None, language, path_prefix, include_tests)
    per_file: list[dict[str, Any]] = []
    total = 0
    scanned = 0
    parse_failures = 0
    for file_row in rows:
        path = file_row["path"]
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            sg_root = SgRoot(text, file_row["language"])
        except Exception:  # noqa: BLE001 - a parse failure is a skipped file, not a crash
            parse_failures += 1
            continue
        scanned += 1
        try:
            found = sg_root.root().find_all(pattern=pattern)
        except Exception as exc:  # noqa: BLE001 - an invalid pattern is reported, not raised mid-scan
            raise ValueError(
                f"invalid ast-grep pattern {pattern!r} for {file_row['language']}: {exc}"
            ) from exc
        if not found:
            continue
        total += len(found)
        per_file.append({"file": path, "language": file_row["language"], "match_count": len(found)})

    per_file.sort(key=lambda entry: (-entry["match_count"], entry["file"]))
    capped = per_file if max_files is None else per_file[:max_files]
    return {
        "files": capped,
        "files_with_matches": len(per_file),
        "files_scanned": scanned,
        "total_matches": total,
        "parse_failures": parse_failures,
        "over_max_files": max_files is not None and len(per_file) > max_files,
    }


def ast_search(
    store: IndexStore,
    root: Path,
    pattern: str,
    language: str | None = None,
    path_prefix: str | None = None,
    include_tests: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Every indexed-file match of an AST pattern (ast-grep syntax: `$VAR`, `$$$VAR`).

    Unlike `search_text`, this matches structure: `pattern="foo($A, $B)"`
    finds a two-argument call to `foo` regardless of formatting, and does
    NOT match `foo(1, 2, 3)` or `bar(1, 2)` -- a regex asked to draw both of
    those distinctions has to either hand-encode the argument-count check
    or accept the false match.

    Args:
        store: The workspace's index.
        root: Workspace root.
        pattern: An ast-grep pattern, e.g. `"foo($A, $B)"` or
            `"if ($COND) { $$$BODY }"`. Must parse as valid syntax in the
            target language(s) -- a fragment good enough to read is not
            always good enough to parse (a bare `return $X` is fine; a bare
            `catch ($E) { $$$B }` is not, in a language that requires the
            enclosing `try`).
        language: Restrict to one of `SUPPORTED_LANGUAGES`; omit to search
            every indexed language the pattern happens to parse in.
        path_prefix: Restrict to a subtree.
        include_tests: Set False to skip files the index marks as tests.
        limit: Max matches returned (`total_matches` still counts every one).

    Returns:
        `{pattern, files_searched, total_matches, returned, truncated,
        matches: [{file, language, line, column, end_line, end_column,
        text, captures, symbol, symbol_kind}], by_file}`. `line`/`end_line`
        are 1-based (ast-grep itself is 0-based; converted here to match
        every other line number this index reports, e.g. `search_text`'s
        `hits[].line`). `column`/`end_column` stay ast-grep's native
        0-based, since nothing else in this index reports a column to be
        consistent with.
    """
    limit = max(1, min(limit, MAX_MATCHES))
    single_vars, multi_vars = _pattern_var_names(pattern)
    rows = _iter_target_files(store, None, language, path_prefix, include_tests)

    matches: list[dict[str, Any]] = []
    by_file: dict[str, int] = {}
    total = 0
    files_searched = 0
    parse_errors: list[dict[str, str]] = []

    for file_row in rows:
        path = file_row["path"]
        try:
            text = (root / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            sg_root = SgRoot(text, file_row["language"])
        except Exception as exc:  # noqa: BLE001 - a parse failure is data, not a crash
            parse_errors.append({"file": path, "reason": str(exc)})
            continue
        files_searched += 1

        try:
            file_matches = sg_root.root().find_all(pattern=pattern)
        except Exception as exc:  # noqa: BLE001 - an invalid pattern is reported, not raised mid-scan
            raise ValueError(f"invalid ast-grep pattern {pattern!r} for {file_row['language']}: {exc}") from exc

        if not file_matches:
            continue

        starts, sym_rows = _symbol_index(store, path)
        for match in file_matches:
            total += 1
            by_file[path] = by_file.get(path, 0) + 1
            if len(matches) >= limit:
                continue
            span = match.range()
            symbol = _enclosing_symbol(starts, sym_rows, span.start.line + 1)
            matches.append(
                {
                    "file": path,
                    "language": file_row["language"],
                    "line": span.start.line + 1,
                    "column": span.start.column,
                    "end_line": span.end.line + 1,
                    "end_column": span.end.column,
                    "text": match.text()[:500],
                    "captures": _captures(match, single_vars, multi_vars),
                    "symbol": (symbol["qualified_name"] or symbol["name"]) if symbol else None,
                    "symbol_kind": symbol["kind"] if symbol else None,
                }
            )

    result: dict[str, Any] = {
        "pattern": pattern,
        "files_searched": files_searched,
        "total_matches": total,
        "returned": len(matches),
        "truncated": total > len(matches),
        "matches": matches,
        "by_file": dict(sorted(by_file.items(), key=lambda kv: -kv[1])[:25]),
    }
    if parse_errors:
        # Reported, never silently swallowed: a file that failed to parse
        # is a file that was NOT searched, and `total_matches` must not be
        # misread as "the whole language was covered".
        result["parse_errors"] = parse_errors
    return result


def ast_replace(
    store: IndexStore,
    root: Path,
    file: str,
    pattern: str,
    replacement: str,
    expected_hash: str,
    language: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    """Rewrite every match of an AST pattern within one file, gated on it being unchanged.

    Same `expected_hash` staleness gate as `replace_symbol`/`insert_lines`
    (`editing.py`'s module docstring): the write happens only if the
    file's current whole-file content still hashes to `expected_hash`.
    Scoped to one file per call, like `replace_symbol`, rather than a
    workspace-wide sweep -- a rewrite touching many files should be
    reviewed file by file, not applied blind across a `by_file` rollup
    from `ast_search`.

    `replacement` uses the same `$VAR`/`$$$VAR` names `pattern` captured
    (ast-grep substitutes them positionally into the replacement text);
    the whole-file diff after substitution is what actually gets checked
    against `expected_hash`'s file, not just the matched spans, so a
    replacement is never applied to text that shifted underneath it.

    Args:
        store: The workspace's index.
        root: Workspace root.
        file: The one file to rewrite, relative to `root`.
        pattern: The ast-grep pattern to match (see `ast_search`).
        replacement: The ast-grep replacement text, referencing `pattern`'s
            metavariables.
        expected_hash: `content_hash` of the file's CURRENT full text
            (e.g. from `get_source_range` over the whole file).
        language: The file's ast-grep language; inferred from the index
            when omitted.
        dry_run: Default True -- returns the plan (match count, preview)
            without writing. Call again with `dry_run=False` to apply;
            like `rename_symbol`, the second call re-reads and re-hashes
            rather than trusting the first one.

    Returns:
        `{file, match_count, applied, new_content_hash?}` — `dry_run=True`
        never sets `new_content_hash` (nothing was written); a zero
        `match_count` is not an error, since "the pattern already does not
        occur" is itself a legitimate, useful answer.
    """
    rows = _iter_target_files(store, file, language, None, include_tests=True)
    if not rows:
        raise LookupError(f"{file!r} is not an indexed file this ast-grep backend covers")
    file_language = rows[0]["language"]

    path = root / file
    original = path.read_text(encoding="utf-8", errors="replace")
    actual = content_hash(original)
    if actual != expected_hash:
        raise StaleEditError(
            f"{file} changed since it was read (expected {expected_hash}, found {actual}); "
            "re-read it and rebuild the replacement"
        )

    sg_root = SgRoot(original, file_language)
    node = sg_root.root()
    try:
        matches = node.find_all(pattern=pattern)
    except Exception as exc:  # noqa: BLE001 - an invalid pattern is reported, not raised
        raise ValueError(f"invalid ast-grep pattern {pattern!r} for {file_language}: {exc}") from exc

    result: dict[str, Any] = {"file": file, "match_count": len(matches), "applied": False}
    if not matches:
        return result

    single_vars, multi_vars = _pattern_var_names(pattern)
    edits = [
        match.replace(_substitute(replacement, match, single_vars, multi_vars)) for match in matches
    ]
    updated = node.commit_edits(edits)
    result["preview"] = updated[:2000] if dry_run else None
    if dry_run:
        return result

    path.write_text(updated, encoding="utf-8")
    store.bump_revision("content", commit=True)
    result["applied"] = True
    result["new_content_hash"] = content_hash(updated)
    result["note"] = "the index is now stale for this file -- run workspace_index"
    return result
