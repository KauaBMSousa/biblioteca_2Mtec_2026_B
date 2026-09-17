"""Local dependency/semantic impact analysis (ENHANCEME §H, §K, §Z).

`impact(symbol)` answers "if I change this, what else moves?" without the
agent orchestrating a `find_references` fan-out by hand and transporting
every row back. It walks the reverse dependency graph transitively from one
symbol and returns *counts first*, then a bounded list of the reached
symbols, the files they live in, and — the point of the whole thing — the
subset of those files the index classifies as tests.

`affected_tests(symbol)` is the same walk narrowed to its useful end: the
test files that (transitively) reach the symbol, plus a ready-to-run
filter expression for `run_tests`.

Both prefer exact semantic edges when a backend has covered the symbol's
file (via `find_dependents`, which already makes that choice and says so
per row), and fall back to the name-matched call graph otherwise. Every
result carries a `resolution` field so a caller never mistakes "the
heuristic graph found nothing" for "nothing depends on this".
"""

from typing import Any

from code_intelligence.core.index.store import IndexStore

#: Hard ceiling on the reverse-BFS frontier, so a hub symbol in a large
#: workspace cannot turn one call into a whole-graph traversal.
_MAX_VISITED = 2000


def _reverse_closure(
    store: IndexStore, symbol_id: str, max_depth: int
) -> tuple[dict[str, int], bool]:
    """Every symbol that transitively depends on `symbol_id`, mapped to its BFS depth.

    Returns `(depth_by_symbol_id, truncated)`. The seed symbol itself is not
    included. `truncated` is True when the `_MAX_VISITED` ceiling was hit.
    """
    from code_intelligence.core.context.context import find_dependents

    depth_by_id: dict[str, int] = {}
    frontier = [symbol_id]
    depth = 0
    truncated = False
    while frontier and depth < max_depth:
        depth += 1
        next_frontier: list[str] = []
        for current in frontier:
            for dependent in find_dependents(store, current):
                dep_id = dependent.get("symbol_id")
                if dep_id is None or dep_id in depth_by_id or dep_id == symbol_id:
                    continue
                depth_by_id[dep_id] = depth
                next_frontier.append(dep_id)
                if len(depth_by_id) >= _MAX_VISITED:
                    return depth_by_id, True
        frontier = next_frontier
    if frontier:
        truncated = True
    return depth_by_id, truncated


def _resolution_for(store: IndexStore, symbol_id: str) -> str:
    """`"exact"` when the seed's file has semantic coverage, else `"heuristic"`."""
    row = store.get_symbol(symbol_id)
    if row is None:
        return "heuristic"
    unit = store.connection.execute(
        "SELECT ok FROM semantic_units WHERE unit = ?", (row["file_path"],)
    ).fetchone()
    return "exact" if unit and unit["ok"] else "heuristic"


def impact(
    store: IndexStore, symbol_id: str, max_depth: int = 5, limit: int = 50
) -> dict[str, Any]:
    """Transitive reverse-dependency impact of changing `symbol_id`.

    Raises `LookupError` when `symbol_id` is not indexed. `max_depth` bounds
    the reverse-BFS; `limit` bounds only the returned `affected_symbols`
    list — every count is complete regardless.
    """
    seed = store.get_symbol(symbol_id)
    if seed is None:
        raise LookupError(f"no indexed symbol {symbol_id!r}")

    depth_by_id, truncated = _reverse_closure(store, symbol_id, max_depth)

    affected: list[dict[str, Any]] = []
    files: set[str] = set()
    test_files: set[str] = set()
    file_rows = {row["path"]: row for row in store.list_files()}
    for dep_id, depth in depth_by_id.items():
        row = store.get_symbol(dep_id)
        if row is None:
            continue
        files.add(row["file_path"])
        file_row = file_rows.get(row["file_path"])
        if file_row and file_row["is_test_file"]:
            test_files.add(row["file_path"])
        affected.append(
            {
                "symbol_id": dep_id,
                "qualified_name": row["qualified_name"] or row["name"],
                "file": row["file_path"],
                "depth": depth,
            }
        )
    affected.sort(key=lambda entry: (entry["depth"], entry["file"], entry["qualified_name"]))

    return {
        "symbol_id": symbol_id,
        "qualified_name": seed["qualified_name"] or seed["name"],
        "file": seed["file_path"],
        "resolution": _resolution_for(store, symbol_id),
        "max_depth": max_depth,
        "truncated": truncated,
        "counts": {
            "affected_symbols": len(depth_by_id),
            "affected_files": len(files),
            "affected_test_files": len(test_files),
        },
        "affected_symbols": affected[:limit],
        "affected_files": sorted(files),
        "affected_test_files": sorted(test_files),
    }


def affected_tests(
    store: IndexStore, symbol_id: str, max_depth: int = 5
) -> dict[str, Any]:
    """The test files that transitively reach `symbol_id`, and how to run just them.

    `filter` is a regex alternation of the test files' stems, suitable for
    `run_tests(filter=...)` (a pytest `-k` / ctest `-R` expression). When no
    test reaches the symbol it is None and `run_tests` should not be
    narrowed — a change with no covering test is a finding, not a shortcut.
    """
    report = impact(store, symbol_id, max_depth=max_depth, limit=0)
    test_files = report["affected_test_files"]
    stems = sorted({path.rsplit("/", 1)[-1].rsplit(".", 1)[0] for path in test_files})
    return {
        "symbol_id": symbol_id,
        "qualified_name": report["qualified_name"],
        "resolution": report["resolution"],
        "truncated": report["truncated"],
        "count": len(test_files),
        "test_files": test_files,
        "filter": "|".join(stems) if stems else None,
        "note": None
        if test_files
        else "no test file reaches this symbol — changing it is unverified",
    }


__all__ = ["impact", "affected_tests"]
