"""Inspection queries an agent needs constantly, answered by the index.

Every function here replaces a script an agent would otherwise write by
hand, run once and throw away -- and those throwaway scripts are where the
mistakes live. The four below were extracted from one real refactoring
session, chosen by how often the same ad-hoc query was rewritten:

    rank_symbols          "what are the worst functions here?"      (~8 times)
    summarize_violations  "how many findings, by rule and area?"    (~10 times)
    file_report           "size, symbols and findings of one file"  (~6 times)
    block_range           "the exact line range of this block"      (3 scripts,
                                                                     4 off-by-one
                                                                     mistakes)

`block_range` is the load-bearing one. Slicing a function out of a file
means knowing exactly where a block starts and ends; counting those lines
by hand -- or by a regex that finds the *first* closing brace -- silently
drops or duplicates code, which is precisely how a refactor introduces a
bug that compiles. The parser already knows the answer, so nobody should
be counting braces.
"""

from pathlib import Path
from typing import Any

from code_intelligence.core.diagnostics.violation import Severity
from code_intelligence.core.index.store import IndexStore

#: Metrics `rank_symbols` can order by, mapped to their column.
RANKABLE_METRICS = {
    "cyclomatic_complexity": "cyclomatic_complexity",
    "loc": "loc",
    "physical_lines": "loc",
}


def rank_symbols(
    store: IndexStore,
    metric: str = "cyclomatic_complexity",
    path_prefix: str | None = None,
    kind: str | None = None,
    limit: int = 20,
) -> dict[str, Any]:
    """The `limit` worst symbols by `metric`, optionally scoped to a subtree.

    "Worst" for triage: which functions to look at first, and which are big
    but simple (a long table) versus long and branchy (real complexity).
    Both numbers come back for every row so the caller can tell those apart
    without a second query.
    """
    column = RANKABLE_METRICS.get(metric)
    if column is None:
        raise ValueError(
            f"unknown metric {metric!r}; choose one of {sorted(RANKABLE_METRICS)}"
        )

    clauses = [f"{column} IS NOT NULL"]
    params: list[Any] = []
    if path_prefix:
        clauses.append("file_path LIKE ?")
        params.append(f"{path_prefix}%")
    if kind:
        clauses.append("kind = ?")
        params.append(kind)

    rows = store.connection.execute(
        f"""SELECT symbol_id, name, qualified_name, kind, file_path, start_line, end_line,
                   loc, cyclomatic_complexity
            FROM symbols WHERE {' AND '.join(clauses)}
            ORDER BY {column} DESC, loc DESC LIMIT ?""",
        [*params, limit],
    ).fetchall()

    return {
        "metric": metric,
        "path_prefix": path_prefix,
        "items": [
            {
                "symbol_id": row["symbol_id"],
                "name": row["qualified_name"] or row["name"],
                "kind": row["kind"],
                "file": row["file_path"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "loc": row["loc"],
                "cyclomatic_complexity": row["cyclomatic_complexity"],
            }
            for row in rows
        ],
    }


def _area_of(path: str, depth: int) -> str:
    """The first `depth` path segments -- the "area" a finding belongs to."""
    parts = path.split("/")
    return "/".join(parts[:depth]) if len(parts) > depth else path


def summarize_violations(
    store: IndexStore,
    path_prefix: str | None = None,
    min_severity: str | None = None,
    area_depth: int = 2,
) -> dict[str, Any]:
    """Counts of findings by severity, by rule and by area -- never the list.

    The question "how bad is this codebase, and where" is asked far more
    often than "show me finding #37", and answering it by paging through
    `get_violations` costs thousands of tokens to produce three numbers.
    """
    floor = Severity[min_severity].value if min_severity else None
    clauses = []
    params: list[Any] = []
    if floor is not None:
        clauses.append("severity >= ?")
        params.append(floor)
    if path_prefix:
        clauses.append("file_path LIKE ?")
        params.append(f"{path_prefix}%")
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

    by_severity: dict[str, int] = {}
    for row in store.connection.execute(
        f"SELECT severity, count(*) n FROM diagnostics{where} GROUP BY severity ORDER BY severity DESC",
        params,
    ):
        by_severity[Severity(row["severity"]).name] = row["n"]

    by_rule: dict[str, int] = {}
    for row in store.connection.execute(
        f"SELECT rule, count(*) n FROM diagnostics{where} GROUP BY rule ORDER BY n DESC", params
    ):
        by_rule[row["rule"]] = row["n"]

    areas: dict[str, int] = {}
    for row in store.connection.execute(
        f"SELECT file_path, count(*) n FROM diagnostics{where} GROUP BY file_path", params
    ):
        area = _area_of(row["file_path"], area_depth)
        areas[area] = areas.get(area, 0) + row["n"]

    worst_files = [
        {"file": row["file_path"], "count": row["n"]}
        for row in store.connection.execute(
            f"SELECT file_path, count(*) n FROM diagnostics{where} "
            "GROUP BY file_path ORDER BY n DESC LIMIT 10",
            params,
        )
    ]

    return {
        "path_prefix": path_prefix,
        "min_severity": min_severity,
        "total": sum(by_severity.values()),
        "by_severity": by_severity,
        "by_rule": by_rule,
        "by_area": dict(sorted(areas.items(), key=lambda kv: -kv[1])),
        "worst_files": worst_files,
    }


def file_report(store: IndexStore, path: str) -> dict[str, Any] | None:
    """One file's size, parse state, symbols and findings, in a single call.

    The three separate queries this replaces were always run together: how
    big is it, what is in it, what is wrong with it.
    """
    row = store.get_file(path)
    if row is None:
        return None

    symbols = [
        {
            "name": symbol["qualified_name"] or symbol["name"],
            "kind": symbol["kind"],
            "start_line": symbol["start_line"],
            "end_line": symbol["end_line"],
            "loc": symbol["loc"],
            "cyclomatic_complexity": symbol["cyclomatic_complexity"],
            "has_doc": symbol["has_doc"],
        }
        for symbol in sorted(
            store.list_symbols_for_file(path),
            key=lambda s: (-(s["loc"] or 0), s["start_line"]),
        )
    ]
    findings = [
        {
            "rule": d["rule"],
            "severity": Severity(d["severity"]).name,
            "line": d["start_line"],
            "message": d["message"],
        }
        for d in store.list_diagnostics(file_path=path)
    ]
    findings.sort(key=lambda f: (-Severity[f["severity"]].value, f["line"] or 0))

    return {
        "file": path,
        "language": row["language"],
        "physical_lines": row["physical_lines"],
        "logical_code_lines": row["logical_code_lines"],
        "comment_lines": row["comment_lines"],
        "parse_ok": bool(row["parse_ok"]),
        "parse_fallback_used": bool(row["parse_fallback_used"]),
        "parse_error": row["parse_error"],
        "is_test_file": bool(row["is_test_file"]),
        "symbol_count": len(symbols),
        "symbols": symbols,
        "finding_count": len(findings),
        "findings": findings,
    }


def block_range(root: Path, path: str, line: int) -> dict[str, Any] | None:
    """The exact line range of the innermost block containing `line`.

    Answers the question every extract-a-function refactor has to answer
    first, and answers it from the parse tree instead of by counting
    braces: which lines *exactly* make up this block, and which of them are
    its body (between the braces) rather than its header.

    Returns the enclosing chain too, outermost last, so a caller that wanted
    the `else` branch but landed inside a nested `if` can walk outwards
    without re-parsing.
    """
    from code_intelligence.core.parser import find_adapter

    file_path = root / path
    if not file_path.is_file():
        return None
    adapter = find_adapter(file_path)
    if adapter is None:
        return None

    source = file_path.read_text(encoding="utf-8", errors="replace")
    chain = adapter.blocks_containing(source, line)
    if not chain:
        # No parse, or a language whose adapter cannot answer. Say so rather
        # than guessing a range: an approximate block boundary is exactly how
        # an extracted function ends up missing a closing brace.
        return None

    lines = source.splitlines()
    # The innermost node containing a line is usually a token (`primitive_type`,
    # an identifier). What a caller slicing code wants is the innermost thing
    # that SPANS lines -- the block. Single-line nodes are kept in `enclosing`
    # for the caller that really wants them.
    spanning = [node for node in chain if node["end_line"] > node["start_line"]]
    innermost = spanning[-1] if spanning else chain[-1]
    position = chain.index(innermost)
    # The body is what sits strictly inside the delimiters, which is what a
    # caller slicing a block wants. Taking the delimiters too is how an
    # extraction ends up unbalanced; leaving them out of the *range* keeps
    # that decision explicit.
    body_start, body_end = innermost["start_line"], innermost["end_line"]
    if body_end > body_start:
        first = lines[body_start - 1].strip()
        last = lines[body_end - 1].strip()
        if first.endswith("{") or first == "{":
            body_start += 1
        if last in ("}", "};", "})", "});"):
            body_end -= 1

    return {
        "file": path,
        "line": line,
        "block": innermost,
        "body_start_line": body_start,
        "body_end_line": body_end,
        "enclosing": list(reversed(chain[:position]))[:8],
    }


def outline_symbol(
    store: IndexStore, root: Path, file: str, symbol: str, max_depth: int = 2
) -> dict[str, Any] | None:
    """The control-flow shape of one symbol, without its text.

    Reading a 703-line function to decide how to split it costs ~7k tokens
    and buries the answer: what the reader needs first is where the seams
    are -- the loops, the branches, the try blocks and how long each runs.
    That is about sixty lines of outline.

    Entries are ordered as they appear, carry their line span and their own
    first line, and nest to `max_depth`.
    """
    from code_intelligence.core.context.editing import _span_of
    from code_intelligence.core.parser import find_adapter

    start, end, row = _span_of(store, file, symbol)
    path = root / file
    adapter = find_adapter(path)
    if adapter is None:
        return None
    source = path.read_text(encoding="utf-8", errors="replace")
    entries = adapter.outline(source, start, end, max_depth)
    if entries is None:
        return None
    return {
        "file": file,
        "symbol": row["qualified_name"] or row["name"],
        "start_line": start,
        "end_line": end,
        "total_lines": end - start + 1,
        "cyclomatic_complexity": row["cyclomatic_complexity"],
        "outline": entries,
    }
