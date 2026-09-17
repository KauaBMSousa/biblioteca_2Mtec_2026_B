"""`context_for_task(symbol)` — the minimum subgraph for a change (ENHANCEME §17).

The agent used to assemble this by hand from `impact` + `find_references`
+ `find_dependencies` + `affected_tests` + `get_symbol`. This does the
whole join once and returns it counts-first, with only the identifiers the
agent needs to navigate — never source.
"""

from typing import Any


def context_for_task(workspace: Any, symbol: str, file: str | None = None) -> dict[str, Any]:
    row = workspace.find_symbol(symbol, file)
    if row is None:
        raise LookupError(f"symbol {symbol!r} is not in the index")

    sid = row["symbol_id"]
    loc = row["location"]
    store = workspace.store

    callers = [
        {"symbol": d.get("qualified_name") or d.get("name"), "file": d.get("file")}
        for d in workspace.find_dependents(sid)
    ]
    callees = [
        {"symbol": d.get("qualified_name") or d.get("name"), "file": d.get("file")}
        for d in workspace.find_dependencies(sid)
    ]

    try:
        impact = workspace.impact(sid)
        impact_counts = impact["counts"]
    except Exception:  # noqa: BLE001
        impact_counts = {}

    try:
        tests = workspace.affected_tests(sid)
        test_files = tests.get("test_files", [])
        test_filter = tests.get("filter")
    except Exception:  # noqa: BLE001
        test_files, test_filter = [], None

    diagnostics = [
        {"rule": d["rule"], "severity": d["severity"], "line": d["start_line"]}
        for d in store.list_diagnostics_for_symbol(sid)
    ]

    recent = None
    try:
        summary = workspace.change_summary()
        recent = row["file"] in summary["changed_files"]
    except Exception:  # noqa: BLE001, S110 - subgraph works without a git repo
        pass

    return {
        "symbol": row["qualified_name"] or row["name"],
        "symbol_id": sid,
        "file": row["file"],
        "language": row.get("language"),
        "range": [loc["start_line"], loc["end_line"]],
        "kind": row["kind"],
        "counts": {
            "callers": len(callers),
            "callees": len(callees),
            "affected_files": impact_counts.get("affected_files", 0),
            "affected_symbols": impact_counts.get("affected_symbols", 0),
            "test_files": len(test_files),
            "diagnostics": len(diagnostics),
        },
        "callers": callers[:50],
        "callees": callees[:50],
        "test_files": sorted(test_files),
        "test_filter": test_filter,
        "diagnostics": diagnostics,
        "changed_since_head": recent,
    }


__all__ = ["context_for_task"]
