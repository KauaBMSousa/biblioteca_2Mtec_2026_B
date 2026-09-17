"""Which symbols nothing calls -- answered from exact edges, or refused.

The question "is this dead?" is the one where a heuristic answer does real
damage. Name matching over a tree-sitter index cannot see a call made
through an object, so it reports live code as uncalled; act on that and you
delete something that runs. Reporting the same shape of answer from exact
edges and from name matching, with only a label to tell them apart, invites
exactly that mistake.

So this module refuses rather than guesses: if the language's semantic
backend has not covered every unit that could contain a call, it raises.
An incomplete exact index is not a smaller truth, it is a different one --
the caller may simply live in a translation unit not extracted yet.
"""

from typing import Any

from code_intelligence.core.index.store import IndexStore

#: Symbol kinds worth asking about. A field or a namespace being
#: "unreferenced" says nothing useful; a function nobody calls does.
_CALLABLE_KINDS = {"function", "method", "constructor", "class", "struct"}


class IncompleteCoverageError(RuntimeError):
    """Raised when the exact index cannot support a dead-code claim."""


def _referenced_symbol_ids(store: IndexStore) -> set[str]:
    """Every indexed symbol that some resolved call reaches.

    Matching a call's target to a symbol by EXACT line looks right and is
    brittle: measured on this project, 273 of 4156 C++ definitions did not
    land on a symbol's `start_line`. Two reasons, and only one is a bug.

    * The signature spans lines, or the structural index and the semantic
      pass saw the file one edit apart -- clang says `validate` is at 213,
      the index says 212. Exact matching then reports a method that `main`
      demonstrably calls as uncalled.
    * The index has no symbol at that line at all (`operator=`, a method
      written inline inside a class in a .cpp). Harmless here: it simply
      marks nothing.

    So the join is CONTAINMENT: a call reaching line L in file F marks
    every symbol whose span covers L -- the method and, for an inline
    definition, the class around it. That errs toward calling things live,
    which is the only direction that is safe to err in when the output is
    a list of things to delete.

    The two arms of the UNION are the two ways a call reaches its target:
    directly (jedi, tsc resolve to the definition) and through a USR (a C++
    call site resolves to the header declaration, never to the .cpp body).
    """
    rows = store.connection.execute(
        """
        WITH referenced(file, line) AS (
            SELECT DISTINCT to_file, to_line FROM semantic_edges
             WHERE to_file IS NOT NULL AND to_line IS NOT NULL
            UNION
            SELECT DISTINCT d.file, d.line FROM semantic_definitions d
              JOIN semantic_edges e ON e.to_usr = d.usr
        )
        SELECT DISTINCT s.symbol_id
          FROM referenced r
          JOIN symbols s
            ON s.file_path = r.file
           AND s.start_line <= r.line
           AND s.end_line >= r.line
        """
    )
    return {row["symbol_id"] for row in rows}


def unreferenced_symbols(
    store: IndexStore,
    pending: dict[str, Any],
    path_prefix: str | None = None,
    language: str | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    """Symbols in scope that no resolved call reaches.

    `pending` is `semantic_coverage()["pending"]`: the check, not a
    decoration. Every language in scope must be fully extracted, or this
    raises with the count still missing.

    The result is a list of CANDIDATES, never a verdict. Four things are
    legitimately unreferenced and alive, and the caller has to judge them:
    entry points (`main`), symbols reached only through virtual dispatch,
    templates instantiated in a unit that failed to parse, and anything
    called from outside the workspace.
    """
    languages_in_scope = (
        [language]
        if language
        else sorted(
            {
                row["language"]
                for row in store.list_files()
                if row["language"] in pending
                and (path_prefix is None or row["path"].startswith(path_prefix))
            }
        )
    )
    # A failed unit blocks the answer just as a pending one does: parsing it
    # is what would have revealed its calls, and it revealed none because it
    # never parsed -- not because there were none.
    incomplete = {
        name: pending[name]
        for name in languages_in_scope
        if not pending.get(name, {}).get("available")
        or pending[name].get("units_pending", 0) > 0
        or pending[name].get("units_failed", 0) > 0
    }
    if incomplete:
        detail = ", ".join(
            f"{name}: {info.get('units_pending', 'no backend')} pending"
            f", {info.get('units_failed', 0)} failed"
            if info.get("available")
            else f"{name}: no backend"
            for name, info in incomplete.items()
        )
        raise IncompleteCoverageError(
            "cannot decide what is unreferenced while the exact index is incomplete "
            f"({detail}). Run semantic_index until semantic_coverage reports "
            "units_pending == 0 and units_failed == 0 for these languages."
        )

    referenced = _referenced_symbol_ids(store)
    files = {
        row["path"]: row["language"]
        for row in store.list_files()
        if (path_prefix is None or row["path"].startswith(path_prefix))
        and (language is None or row["language"] == language)
    }

    candidates: list[dict[str, Any]] = []
    scanned = 0
    for path in sorted(files):
        for symbol in store.list_symbols_for_file(path):
            if symbol["kind"] not in _CALLABLE_KINDS:
                continue
            scanned += 1
            if symbol["symbol_id"] in referenced:
                continue
            candidates.append(
                {
                    "symbol_id": symbol["symbol_id"],
                    "qualified_name": symbol["qualified_name"] or symbol["name"],
                    "kind": symbol["kind"],
                    "file": path,
                    "line": symbol["start_line"],
                    "loc": symbol["loc"],
                    "language": files[path],
                }
            )

    candidates.sort(key=lambda row: (-row["loc"], row["file"], row["line"]))
    return {
        "resolution": "exact",
        "languages": languages_in_scope,
        "symbols_scanned": scanned,
        "candidates_total": len(candidates),
        "candidates": candidates[:limit],
        "caveats": [
            "entry points are never called from inside the workspace",
            "virtual dispatch resolves to the base declaration, not the override",
            "a template instantiated only in a unit that failed to parse looks dead",
            "callers outside the workspace are invisible to the index",
        ],
    }
