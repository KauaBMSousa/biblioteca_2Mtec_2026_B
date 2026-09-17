"""Storage for exactly-resolved references, and the staleness rules for them.

Kept apart from `dependencies` (name-matched guesses) on purpose: the only
reason to pay for semantic extraction is that its edges are known to be
right, and mixing the two would throw that away.

The staleness rule is the interesting part. A unit's edges are stale when
the unit's own text changed OR when any file it read changed -- editing a
C++ header does not touch the .cpp that includes it, but it can change what
every call in that .cpp resolves to. `stale_units` implements exactly that,
so "exact" never quietly means "exact as of some earlier version".
"""

import time
from typing import Any

from code_intelligence.core.index.store import IndexStore


def replace_unit(
    store: IndexStore,
    unit: str,
    language: str,
    backend: str,
    unit_hash: str,
    edges: list[dict[str, Any]],
    depends_on: list[str],
    ok: bool = True,
    error: str | None = None,
    definitions: list[tuple[str, str, int, str]] | None = None,
) -> None:
    """Record one unit's extraction result, replacing whatever was there."""
    connection = store.connection
    connection.execute("DELETE FROM semantic_edges WHERE from_file = ?", (unit,))
    connection.execute("DELETE FROM semantic_unit_deps WHERE unit = ?", (unit,))
    connection.execute("DELETE FROM semantic_units WHERE unit = ?", (unit,))
    connection.execute("DELETE FROM semantic_definitions WHERE unit = ?", (unit,))

    connection.execute(
        "INSERT INTO semantic_units (unit, language, backend, unit_hash, ok, error, extracted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (unit, language, backend, unit_hash, 1 if ok else 0, error,
         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())),
    )
    for edge in edges:
        connection.execute(
            "INSERT INTO semantic_edges "
            "(from_file, from_line, from_column, to_name, to_file, to_line, to_usr, kind, backend, language) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                edge["from_file"], edge["from_line"], edge["from_column"], edge["to_name"],
                edge.get("to_file"), edge.get("to_line"), edge.get("to_usr"),
                edge.get("kind", "call"), backend, language,
            ),
        )
    for usr, file, line, name in definitions or []:
        connection.execute(
            "INSERT INTO semantic_definitions (usr, file, line, name, unit, language) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (usr, file, line, name, unit, language),
        )
    for dependency in dict.fromkeys(depends_on):
        connection.execute(
            "INSERT INTO semantic_unit_deps (unit, depends_on, language) VALUES (?, ?, ?)",
            (unit, dependency, language),
        )


def stale_units(store: IndexStore, changed_files: set[str]) -> set[str]:
    """Units whose semantic edges no longer describe the code on disk.

    A unit is stale when it is itself among the changed files, or when it
    READ one of them. The second half is what makes header edits safe: the
    .cpp did not change, but what its calls resolve to may have.
    """
    if not changed_files:
        return set()
    stale: set[str] = set()
    placeholders = ",".join("?" for _ in changed_files)
    ordered = list(changed_files)

    for row in store.connection.execute(
        f"SELECT unit FROM semantic_units WHERE unit IN ({placeholders})", ordered
    ):
        stale.add(row["unit"])
    for row in store.connection.execute(
        f"SELECT unit FROM semantic_unit_deps WHERE depends_on IN ({placeholders})", ordered
    ):
        stale.add(row["unit"])
    return stale


def forget_units(store: IndexStore, units: set[str]) -> None:
    """Drop everything recorded for `units` (they are about to be re-extracted)."""
    for unit in units:
        store.connection.execute("DELETE FROM semantic_edges WHERE from_file = ?", (unit,))
        store.connection.execute("DELETE FROM semantic_unit_deps WHERE unit = ?", (unit,))
        store.connection.execute("DELETE FROM semantic_units WHERE unit = ?", (unit,))


def references_to(store: IndexStore, file: str, line: int) -> list[dict[str, Any]]:
    """Exact call sites resolving to the definition at `file`:`line`.

    Two joins, because two things can be true. A backend that resolves
    straight to the definition (jedi, tsc) produces edges whose
    `to_file`/`to_line` ARE this location. A C++ call site instead sees the
    header declaration, so its edge points there -- the definition is
    reached through the USR both share, which is what the second query
    does. Without it, "who calls this method?" is answered "nobody" for
    every C++ method defined in a .cpp.
    """
    direct = store.connection.execute(
        "SELECT from_file, from_line, from_column, to_name, kind, backend, language "
        "FROM semantic_edges WHERE to_file = ? AND to_line = ?",
        (file, line),
    ).fetchall()

    by_usr = store.connection.execute(
        "SELECT e.from_file, e.from_line, e.from_column, e.to_name, e.kind, e.backend, e.language "
        "FROM semantic_edges e "
        "JOIN semantic_definitions d ON d.usr = e.to_usr "
        "WHERE d.file = ? AND d.line = ?",
        (file, line),
    ).fetchall()

    seen: set[tuple[str, int, int]] = set()
    results: list[dict[str, Any]] = []
    for row in [*direct, *by_usr]:
        key = (row["from_file"], row["from_line"], row["from_column"])
        if key in seen:
            continue
        seen.add(key)
        results.append(dict(row))
    results.sort(key=lambda row: (row["from_file"], row["from_line"]))
    return results


def references_to_name(store: IndexStore, name: str, language: str | None = None) -> list[dict[str, Any]]:
    """Exact call sites of everything resolving to `name` (a fallback view)."""
    query = (
        "SELECT from_file, from_line, from_column, to_name, to_file, to_line, kind, backend, language "
        "FROM semantic_edges WHERE to_name = ?"
    )
    params: list[Any] = [name]
    if language:
        query += " AND language = ?"
        params.append(language)
    return [dict(row) for row in store.connection.execute(query + " ORDER BY from_file, from_line", params)]


def coverage(store: IndexStore) -> dict[str, Any]:
    """How much of the index is backed by exact data, and how much failed.

    Reported per language so an agent can tell "no exact backend for this
    language" from "the backend ran and found nothing".
    """
    per_language: dict[str, dict[str, Any]] = {}
    for row in store.connection.execute(
        "SELECT language, backend, count(*) n, sum(ok) ok_count FROM semantic_units GROUP BY language, backend"
    ):
        per_language[row["language"]] = {
            "backend": row["backend"],
            "units": row["n"],
            "units_ok": row["ok_count"] or 0,
            "units_failed": row["n"] - (row["ok_count"] or 0),
        }
    total_edges = store.connection.execute("SELECT count(*) c FROM semantic_edges").fetchone()["c"]
    failures = [
        dict(row)
        for row in store.connection.execute(
            "SELECT unit, language, error FROM semantic_units WHERE ok = 0 LIMIT 20"
        )
    ]
    return {"languages": per_language, "total_edges": total_edges, "failures": failures}
