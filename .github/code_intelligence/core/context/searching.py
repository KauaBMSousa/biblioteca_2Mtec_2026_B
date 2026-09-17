"""Text search that knows what it is searching.

`grep` was, by a wide margin, the most-run command of a real refactoring
session: 284 of 1230 shell calls. Every one of them had the same two
problems.

**It answers with the wrong unit.** `file:line: text` is a position in a
file; what a reader needs is a position in the *code* -- which function is
this in? Answering that took a second command (or a whole-file read) per
interesting hit, so a search for a symbol used in twelve places cost twelve
follow-ups.

**It searches the wrong files.** Raw grep walks whatever the glob catches:
build trees, `.venv`, vendored copies, the 9473 headers under a Python
virtualenv that made a "9949 C++ files" count out of a codebase with 479.
The index already knows which files are real source; searching that list is
both faster and correct by construction.

So this returns the enclosing symbol with every hit, plus rollups by symbol
and by file, so a broad search answers "where does this concept live?"
without printing three hundred lines of matches.
"""

import re
from bisect import bisect_right
from pathlib import Path
from typing import Any

from code_intelligence.core.index.store import IndexStore

#: Hard cap on returned hits, whatever `limit` asks for: a search that
#: matches ten thousand lines is a question that needs narrowing, not a
#: ten-thousand-line answer.
MAX_HITS = 500


def _symbol_index(store: IndexStore, path: str) -> tuple[list[int], list[dict[str, Any]]]:
    """Symbols of one file as (sorted start lines, rows) for bisect lookup."""
    rows = sorted(
        (row for row in store.list_symbols_for_file(path) if row["start_line"]),
        key=lambda row: row["start_line"],
    )
    return [row["start_line"] for row in rows], rows


def _enclosing_symbol(starts: list[int], rows: list[dict[str, Any]], line: int) -> dict[str, Any] | None:
    """The innermost symbol whose span contains `line`.

    Symbols nest (a method inside a class), so the last one that starts
    at-or-before the line and still ends after it wins -- walking backwards
    from the bisect point rather than taking the first hit, which would
    report the class where the method is the useful answer.
    """
    position = bisect_right(starts, line) - 1
    best: dict[str, Any] | None = None
    while position >= 0:
        row = rows[position]
        if row["end_line"] is None or row["end_line"] >= line:
            if best is None or (row["start_line"] >= best["start_line"]):
                best = row
            break
        position -= 1
    return best


def search_text(
    store: IndexStore,
    root: Path,
    pattern: str,
    path_prefix: str | None = None,
    language: str | None = None,
    include_tests: bool = True,
    is_regex: bool = True,
    case_sensitive: bool = True,
    limit: int = 50,
) -> dict[str, Any]:
    """Search the indexed source files, reporting each hit with its symbol.

    Only files the index knows about are searched, which is what keeps
    build trees, virtualenvs and vendored copies out of the answer.
    """
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        matcher = re.compile(pattern if is_regex else re.escape(pattern), flags)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc

    limit = max(1, min(limit, MAX_HITS))
    hits: list[dict[str, Any]] = []
    by_symbol: dict[str, int] = {}
    by_file: dict[str, int] = {}
    total = 0
    files_searched = 0

    for file_row in store.list_files():
        path = file_row["path"]
        if path_prefix and not path.startswith(path_prefix):
            continue
        if language and file_row["language"] != language:
            continue
        if not include_tests and file_row["is_test_file"]:
            continue

        full_path = root / path
        try:
            text = full_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        files_searched += 1

        starts: list[int] | None = None
        rows: list[dict[str, Any]] | None = None
        for number, line in enumerate(text.splitlines(), start=1):
            if not matcher.search(line):
                continue
            total += 1
            by_file[path] = by_file.get(path, 0) + 1
            if starts is None:
                starts, rows = _symbol_index(store, path)
            symbol = _enclosing_symbol(starts, rows, number)
            symbol_name = (symbol["qualified_name"] or symbol["name"]) if symbol else None
            key = f"{path}:{symbol_name}" if symbol_name else path
            by_symbol[key] = by_symbol.get(key, 0) + 1
            if len(hits) < limit:
                hits.append(
                    {
                        "file": path,
                        "line": number,
                        "text": line.strip()[:300],
                        "symbol": symbol_name,
                        "symbol_kind": symbol["kind"] if symbol else None,
                        "symbol_start_line": symbol["start_line"] if symbol else None,
                        "symbol_end_line": symbol["end_line"] if symbol else None,
                    }
                )

    return {
        "pattern": pattern,
        "files_searched": files_searched,
        "total_hits": total,
        "returned": len(hits),
        "truncated": total > len(hits),
        "hits": hits,
        "by_symbol": dict(sorted(by_symbol.items(), key=lambda kv: -kv[1])[:25]),
        "by_file": dict(sorted(by_file.items(), key=lambda kv: -kv[1])[:25]),
    }
