"""Structural edits: replace a symbol, insert at a line -- with a staleness gate.

The second-largest slice of a real refactoring session (207 of 1230 shell
calls) was the agent writing throwaway Python to edit a file:

    s = path.read_text()
    s = s.replace(old, new)      # or s[:start] + new + s[end:]
    path.write_text(s)

Three things go wrong with that, and all three happened:

* `s.index(anchor)` finds the FIRST occurrence, which is not always the
  intended one -- once it matched text the same script had just inserted,
  so the edit "succeeded" and changed nothing;
* line ranges computed by hand are off by one at the opening brace, then
  at the closing one;
* nothing checks that the file still looks the way the agent last read it,
  so a stale plan silently overwrites newer content.

The operations here take the unit the agent is actually thinking in -- a
symbol -- and refuse to act on a file that has changed underneath, the same
`expected_hash` gate `validate_patch` already applies to diffs.
"""

import re
from pathlib import Path
from typing import Any

from code_intelligence.core.hashes.content_hash import content_hash
from code_intelligence.core.index.store import IndexStore
from code_intelligence.core.semantic.backends import CONFIDENCE_EXACT


class StaleEditError(RuntimeError):
    """The target's current content does not match `expected_hash`.

    Raised rather than merged or forced: the caller's picture of the file is
    out of date, and guessing which version was intended is exactly the kind
    of silent overwrite this gate exists to prevent.
    """


class LowConfidenceRenameError(RuntimeError):
    """`rename_symbol` refuses to rewrite call sites from name-matching alone.

    `core/semantic/backends.py` exists because "is this safe to rename?"
    is one of the two questions the tree-sitter index cannot answer on its
    own -- a heuristic caller list can both miss real call sites (renaming
    the definition but not every use) and hit unrelated ones (a same-named
    method on a different class). A rename that silently rewrote both would
    be exactly the wrong kind of confident. Raised when no exact semantic
    backend has analyzed the definition's file; the caller should run
    `refresh_semantic`/`workspace_index(semantic=True)` for that language
    first, or fall back to `search_text` + `replace_symbol` by hand.
    """


_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _span_of(store: IndexStore, file: str, symbol: str) -> tuple[int, int, dict[str, Any]]:
    """Locate `symbol` within `file`, by symbol_id, qualified name or bare name.

    Matches against the file's own symbols rather than guessing which name
    column the language uses: C++ stores a method as `Owner::method` in both
    `name` and `qualified_name`, Python stores `Owner.method` qualified and
    `method` bare. Searching by a name shape that the language does not use
    silently finds nothing -- which is exactly what happened the first time
    `outline_symbol` was pointed at a C++ method.
    """
    row = store.get_symbol(symbol)
    if row is not None and row["file_path"] == file:
        return row["start_line"], row["end_line"], row

    candidates = store.list_symbols_for_file(file)
    if not candidates:
        raise LookupError(f"{file} has no indexed symbols (never indexed, or a parse fallback)")

    # Tiered, most-specific first: a bare `alpha` means the top-level
    # `alpha`, not `Holder.alpha` -- even though the method's short name
    # also reads "alpha". Only when nothing matches qualified does the
    # search widen, so a name is never ambiguous by accident of tiering.
    tail = symbol.split("::")[-1].split(".")[-1]
    tiers = (
        [c for c in candidates if symbol in (c["symbol_id"], c["qualified_name"])],
        [c for c in candidates if c["name"] == symbol],
        [c for c in candidates if tail in (c["qualified_name"], c["name"])],
    )
    matching = next((tier for tier in tiers if tier), [])
    if not matching:
        raise LookupError(f"no symbol {symbol!r} in {file}")
    if len(matching) > 1:
        names = sorted(
            f"{c['qualified_name'] or c['name']} (line {c['start_line']})" for c in matching
        )
        raise LookupError(f"{symbol!r} is ambiguous in {file}: {names}")
    row = matching[0]
    return row["start_line"], row["end_line"], row


def symbol_source(store: IndexStore, root: Path, file: str, symbol: str) -> dict[str, Any]:
    """One symbol's exact source text plus the hash needed to edit it.

    The read half of `replace_symbol`: what you get back is what you pass
    as `expected_hash`, so a read-then-write round trip cannot straddle
    someone else's change.
    """
    start, end, row = _span_of(store, file, symbol)
    lines = (root / file).read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(lines[start - 1 : end])
    return {
        "file": file,
        "symbol": row["qualified_name"] or row["name"],
        "kind": row["kind"],
        "start_line": start,
        "end_line": end,
        "content_hash": content_hash(text),
        "source": text,
    }


def replace_symbol(
    store: IndexStore, root: Path, file: str, symbol: str, new_text: str, expected_hash: str
) -> dict[str, Any]:
    """Replace one symbol's whole definition with `new_text`.

    `expected_hash` must be the hash of the symbol's CURRENT text (from
    `symbol_source`). The write happens only if it still matches.
    """
    start, end, _row = _span_of(store, file, symbol)
    path = root / file
    original = path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines()
    current = "\n".join(lines[start - 1 : end])
    actual = content_hash(current)
    if actual != expected_hash:
        raise StaleEditError(
            f"{file}:{symbol} changed since it was read "
            f"(expected {expected_hash}, found {actual}); re-read it and rebuild the edit"
        )

    replacement = new_text.rstrip("\n").splitlines()
    updated = lines[: start - 1] + replacement + lines[end:]
    trailing = "\n" if original.endswith("\n") else ""
    path.write_text("\n".join(updated) + trailing, encoding="utf-8")
    store.bump_revision("content", commit=True)

    return {
        "file": file,
        "symbol": symbol,
        "replaced_lines": [start, end],
        "new_line_count": len(replacement),
        "line_delta": len(replacement) - (end - start + 1),
        "new_content_hash": content_hash("\n".join(replacement)),
        "note": "the index is now stale for this file -- run workspace_index",
    }


def write_file(
    store: IndexStore, root: Path, file: str, new_text: str, expected_hash: str
) -> dict[str, Any]:
    """Replace a file's ENTIRE contents, gated on its current text matching `expected_hash`.

    The whole-file counterpart of `replace_symbol` — used by the
    higher-level refactoring primitives (`add_import`, `extract_function`)
    whose CST rewrite produces a new file, not a new span. Not exposed as a
    standalone tool: raw whole-file writes are the fallback §U warns
    against, and these primitives only reach it from inside a transaction.
    """
    path = root / file
    original = path.read_text(encoding="utf-8", errors="replace") if path.exists() else ""
    actual = content_hash(original)
    if actual != expected_hash:
        raise StaleEditError(
            f"{file} changed since it was read (expected {expected_hash}, found {actual}); "
            "re-read it and rebuild the edit"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(new_text, encoding="utf-8")
    store.bump_revision("content", commit=True)
    return {
        "file": file,
        "new_content_hash": content_hash(new_text),
        "note": "the index is now stale for this file -- run workspace_index",
    }


def insert_lines(
    store: IndexStore,
    root: Path,
    file: str,
    line: int,
    text: str,
    expected_hash: str,
    anchor_lines: int = 3,
) -> dict[str, Any]:
    """Insert `text` BEFORE `line`, gated on the surrounding lines being unchanged.

    The gate covers `anchor_lines` on BOTH sides of the insertion point,
    rather than the whole file: an unrelated edit elsewhere does not block
    the write, while a change to the very place being anchored on does.

    Both sides, not just the preceding ones, because an insertion does not
    alter the lines before it -- gating on those alone would let the same
    insertion be applied twice without complaint.
    """
    path = root / file
    original = path.read_text(encoding="utf-8", errors="replace")
    lines = original.splitlines()
    if not 1 <= line <= len(lines) + 1:
        raise ValueError(f"line {line} is outside {file} (1..{len(lines) + 1})")

    actual = content_hash(_anchor_text(lines, line, anchor_lines))
    if actual != expected_hash:
        raise StaleEditError(
            f"{file}:{line} anchor changed since it was read "
            f"(expected {expected_hash}, found {actual}); re-read and retry"
        )

    inserted = text.rstrip("\n").splitlines()
    updated = lines[: line - 1] + inserted + lines[line - 1 :]
    trailing = "\n" if original.endswith("\n") else ""
    path.write_text("\n".join(updated) + trailing, encoding="utf-8")
    store.bump_revision("content", commit=True)
    return {
        "file": file,
        "inserted_at_line": line,
        "line_count": len(inserted),
        "note": "the index is now stale for this file -- run workspace_index",
    }


def _anchor_text(lines: list[str], line: int, anchor_lines: int) -> str:
    """The lines on both sides of an insertion point, as one string."""
    before = lines[max(0, line - 1 - anchor_lines) : line - 1]
    after = lines[line - 1 : line - 1 + anchor_lines]
    return "\n".join([*before, *after])


def anchor_hash(root: Path, file: str, line: int, anchor_lines: int = 3) -> dict[str, Any]:
    """The hash `insert_lines` will expect for an insertion before `line`."""
    lines = (root / file).read_text(encoding="utf-8", errors="replace").splitlines()
    anchor = _anchor_text(lines, line, anchor_lines)
    return {
        "file": file,
        "line": line,
        "anchor_lines": anchor_lines,
        "anchor": anchor,
        "content_hash": content_hash(anchor),
    }


def _plan_line_edit(root: Path, file: str, line: int, old_name: str, new_name: str) -> dict[str, Any]:
    """Whether `old_name` can be swapped for `new_name` on `file`:`line` without guessing.

    Deliberately does not trust a semantic backend's reported column -- its
    0-based/1-based, token-start/cursor-position conventions differ per
    backend (jedi vs. clangd vs. ts-morph), and getting that wrong would
    silently corrupt a line. Instead: the reference graph says WHICH line;
    a whole-word regex on that line's actual current text says WHAT to
    change, and only commits to it when it is unambiguous:

    * zero matches -- the line changed since the edge was recorded (`stale`)
    * two or more matches -- which one was the referenced use is not
      determinable from a line number alone (`ambiguous`); left for the
      caller to resolve with `replace_symbol`
    * exactly one match -- the only case this reports `"ok"`
    """
    path = root / file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        return {"file": file, "line": line, "status": "missing", "reason": str(exc)}
    if not 1 <= line <= len(lines):
        return {"file": file, "line": line, "status": "missing", "reason": "line is outside the file"}

    current = lines[line - 1]
    matches = list(re.finditer(rf"\b{re.escape(old_name)}\b", current))
    if not matches:
        return {
            "file": file, "line": line, "status": "stale", "current_text": current,
            "reason": f"{old_name!r} is no longer on this line -- it changed since the reference was recorded",
        }
    if len(matches) > 1:
        return {
            "file": file, "line": line, "status": "ambiguous", "current_text": current,
            "reason": f"{old_name!r} appears {len(matches)} times on this line -- which one is not determinable from a line number alone",
        }
    match = matches[0]
    return {
        "file": file,
        "line": line,
        "status": "ok",
        "current_text": current,
        "new_text": current[: match.start()] + new_name + current[match.end() :],
    }


def _apply_line_edits(root: Path, plans: list[dict[str, Any]]) -> None:
    """Write every already-verified (`status == "ok"`) plan, one read/write pass per file.

    Grouped by file rather than done plan-by-plan so that two edits landing
    in the same file (a definition and a same-file call site, say) both
    apply against the file's ORIGINAL line list -- each plan's `new_text`
    was computed against a single-line substitution, which never changes
    the file's line count, so applying every target file's edits against
    one read is safe and never shifts a later plan's line number out from
    under it.
    """
    by_file: dict[str, list[dict[str, Any]]] = {}
    for plan in plans:
        by_file.setdefault(plan["file"], []).append(plan)
    for file, file_plans in by_file.items():
        path = root / file
        original = path.read_text(encoding="utf-8", errors="replace")
        lines = original.splitlines()
        for plan in file_plans:
            lines[plan["line"] - 1] = plan["new_text"]
        trailing = "\n" if original.endswith("\n") else ""
        path.write_text("\n".join(lines) + trailing, encoding="utf-8")


def rename_symbol(
    store: IndexStore, root: Path, file: str, symbol: str, new_name: str, dry_run: bool = True
) -> dict[str, Any]:
    """Rename `symbol`'s definition and every call site an exact semantic backend can find.

    Refuses outright (`LowConfidenceRenameError`) unless the definition's
    file has exact, type-aware coverage (see `core/semantic/backends.py`)
    -- a heuristic caller list doesn't even carry a call-site line number
    (only "this symbol references that one"), so there is no safe way to
    locate the token to rewrite; guessing "the first occurrence of the old
    name in that symbol's body" is precisely the bug class `editing.py`'s
    module docstring exists to avoid repeating.

    Unlike `replace_symbol`/`insert_lines`, there is no `expected_hash` to
    pass: every site (definition and each call site) is re-read and
    re-verified in THIS call, immediately before writing, so there is no
    gap between "plan" and "act" for anything to go stale inside. Call
    once with `dry_run=True` (the default) to see the plan; call again
    with `dry_run=False` to apply it -- the second call re-derives the plan
    fresh rather than trusting the first one, so anything that changed in
    between shows up as a new `blocked`/`blocking_reasons` result instead
    of being silently overwritten.

    A rename is applied only when EVERY site (the definition plus every
    call site) resolves unambiguously; if even one is `stale` or
    `ambiguous`, nothing is written and `blocking_reasons` says exactly
    which sites need `replace_symbol` by hand instead.

    Known limitation: only sites the semantic backend records as a
    call/reference edge are touched -- a class name used purely as a type
    annotation or mentioned in a docstring/comment is not.
    """
    from code_intelligence.core.index import semantic_store

    if not _IDENTIFIER_RE.match(new_name):
        raise ValueError(f"{new_name!r} is not a valid identifier")

    start, _end, row = _span_of(store, file, symbol)
    old_name = row["name"]
    if old_name == new_name:
        raise ValueError(f"{symbol!r} is already named {new_name!r}")

    unit_row = store.connection.execute(
        "SELECT ok FROM semantic_units WHERE unit = ?", (file,)
    ).fetchone()
    if not (unit_row and unit_row["ok"]):
        raise LowConfidenceRenameError(
            f"{file} has no exact (type-aware) semantic coverage -- rename refuses to guess "
            "call sites from name-matching alone. Run refresh_semantic for this language first, "
            "or edit call sites individually with search_text + replace_symbol."
        )

    edges = semantic_store.references_to(store, file, start)
    seen_sites: set[tuple[str, int]] = set()
    call_site_plans: list[dict[str, Any]] = []
    for edge in edges:
        key = (edge["from_file"], edge["from_line"])
        if key in seen_sites:
            continue
        seen_sites.add(key)
        plan = _plan_line_edit(root, edge["from_file"], edge["from_line"], old_name, new_name)
        plan["backend"] = edge["backend"]
        call_site_plans.append(plan)

    definition_plan = _plan_line_edit(root, file, start, old_name, new_name)
    all_plans = [definition_plan, *call_site_plans]
    blocking = [p for p in all_plans if p["status"] != "ok"]

    result: dict[str, Any] = {
        "symbol": row["qualified_name"] or row["name"],
        "old_name": old_name,
        "new_name": new_name,
        "confidence": CONFIDENCE_EXACT,
        "definition": definition_plan,
        "call_sites": call_site_plans,
        "call_site_count": len(call_site_plans),
        "blocked": bool(blocking),
        "applied": False,
    }
    if blocking:
        result["blocking_reasons"] = [
            {"file": p["file"], "line": p["line"], "status": p["status"], "reason": p["reason"]}
            for p in blocking
        ]
        return result
    if dry_run:
        return result

    _apply_line_edits(root, all_plans)
    store.bump_revision("content", commit=True)
    result["applied"] = True
    result["note"] = "the index is now stale for every file touched -- run workspace_index"
    return result
