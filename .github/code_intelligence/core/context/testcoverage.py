"""Which functions no test ever reaches -- answered from exact edges, or refused.

"Does this function have a test?" looks like a question a name search can
answer, and it cannot. A test that exercises `Config::load` may never write
the string `Config::load`: it calls a wrapper, or a fixture, or the symbol
under its qualified name in one file and its unqualified name in another.
Name matching therefore reports tested code as untested, and the reader
learns to ignore the rule -- which is worse than not having it.

So this module asks the semantic index the same way `deadcode` does, with
one extra condition: the call must originate in a file the index classifies
as a test. And it refuses on the same terms. An exact index that has not
covered every unit cannot support "nothing tests this": the test may simply
live in a translation unit not extracted yet.

The failure mode this guards against is the loud one, and it is worth
naming. `is_test_file` is decided by filename patterns, and this project's
3097 tests live in `*_gtest.cpp` files that the conventional `*_test.cpp`
pattern does not match. With that misconfiguration every symbol in the
workspace comes back untested -- a result that is obviously wrong, which is
why it is survivable. The dangerous direction is the other one, and it does
not occur here: this rule never reports a symbol as tested unless a test
file really does reach it.
"""

from typing import Any

from code_intelligence.core.context.deadcode import IncompleteCoverageError
from code_intelligence.core.index.store import IndexStore

#: Kinds a test can meaningfully target. A field or a namespace has no
#: behaviour to pin; a function or a method does. Constructors are excluded
#: by default because they are exercised by every test of the type, and
#: flagging them adds a finding per class that no one will act on.
_TESTABLE_KINDS = {"function", "method"}

#: Entry points are invoked by the operating system, never by a test.
#: Flagging them produces one unactionable finding per binary.
_ENTRY_POINT_NAMES = {"main", "__main__", "wmain"}


def test_covered_symbol_ids(store: IndexStore) -> set[str]:
    """Every indexed symbol that some call FROM A TEST FILE reaches.

    Identical in shape to `deadcode._referenced_symbol_ids`, and different in
    exactly one clause -- the join to `files` that requires the CALLER to be a
    test. Keeping the two queries side by side rather than sharing a
    parameterised one is deliberate: they answer different questions and are
    allowed to drift apart, and a single query with a boolean flag would hide
    that a change to "is it dead" silently changes "is it tested".

    The two arms of the UNION are the two ways a call reaches its target:
    directly (jedi and tsc resolve to the definition) and through a USR (a
    C++ test calls the header DECLARATION; the index holds the .cpp body, so
    file:line cannot match them and the USR can).

    The join to `symbols` is CONTAINMENT, not equality on `start_line`: a
    call landing anywhere inside a symbol's span marks that symbol. Exact
    line matching misses roughly 7% of C++ definitions (multi-line
    signatures, or the structural and semantic passes seeing the file one
    edit apart), and every miss here would be reported as an untested
    function that is in fact tested.
    """
    rows = store.connection.execute(
        """
        WITH tested(file, line) AS (
            SELECT DISTINCT e.to_file, e.to_line
              FROM semantic_edges e
              JOIN files f ON f.path = e.from_file
             WHERE f.is_test_file = 1
               AND e.to_file IS NOT NULL
               AND e.to_line IS NOT NULL
            UNION
            SELECT DISTINCT d.file, d.line
              FROM semantic_definitions d
              JOIN semantic_edges e ON e.to_usr = d.usr
              JOIN files f ON f.path = e.from_file
             WHERE f.is_test_file = 1
        )
        SELECT DISTINCT s.symbol_id
          FROM tested t
          JOIN symbols s
            ON s.file_path = t.file
           AND s.start_line <= t.line
           AND s.end_line >= t.line
        """
    )
    return {row["symbol_id"] for row in rows}


def incomplete_languages(
    pending: dict[str, Any], languages_in_scope: list[str]
) -> dict[str, Any]:
    """The languages whose exact index cannot support a coverage claim.

    A FAILED unit blocks the answer exactly as a PENDING one does. Parsing it
    is what would have revealed its calls, and it revealed none because it
    never parsed -- not because there were none.
    """
    return {
        name: pending[name]
        for name in languages_in_scope
        if not pending.get(name, {}).get("available")
        or pending[name].get("units_pending", 0) > 0
        or pending[name].get("units_failed", 0) > 0
    }


def describe_incompleteness(incomplete: dict[str, Any]) -> str:
    """One line naming what is missing and what to run to fix it."""
    detail = ", ".join(
        f"{name}: {info.get('units_pending', 'no backend')} pending"
        f", {info.get('units_failed', 0)} failed"
        if info.get("available")
        else f"{name}: no backend"
        for name, info in sorted(incomplete.items())
    )
    return (
        f"cannot decide what is untested while the exact index is incomplete ({detail}). "
        "Run semantic_index until semantic_coverage reports units_pending == 0 and "
        "units_failed == 0 for these languages."
    )


def _is_private(name: str) -> bool:
    """Leading-underscore convention (Python, JavaScript).

    C++ and Java express privacy in the type, not the name, so this catches
    nothing there -- which is correct: a C++ private method is still part of
    a class whose public surface a test drives, and the containment join
    already credits it when a test reaches it.
    """
    return name.startswith("_")


def untested_symbols(
    store: IndexStore,
    pending: dict[str, Any],
    path_prefix: str | None = None,
    language: str | None = None,
    min_loc: int = 5,
    include_private: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    """Functions and methods that no test file reaches.

    `pending` is `semantic_coverage()["pending"]`: the check, not a
    decoration. Every language in scope must be fully extracted, or this
    raises `IncompleteCoverageError` with the count still missing.

    The result is a list of CANDIDATES, never a verdict. Three things are
    legitimately untested-looking and fine, and the caller has to judge them:
    a symbol reached only through virtual dispatch (the edge points at the
    base declaration), a symbol whose only test lives outside the workspace,
    and a thin delegator whose behaviour is pinned by the test of what it
    delegates to.
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
    incomplete = incomplete_languages(pending, languages_in_scope)
    if incomplete:
        raise IncompleteCoverageError(describe_incompleteness(incomplete))

    covered = test_covered_symbol_ids(store)
    files = {
        row["path"]: row
        for row in store.list_files()
        if (path_prefix is None or row["path"].startswith(path_prefix))
        and (language is None or row["language"] == language)
    }

    candidates: list[dict[str, Any]] = []
    scanned = 0
    tested = 0
    for path in sorted(files):
        file_row = files[path]
        # A test's own helpers do not need tests, and neither does generated
        # code -- rewriting it is the fix there, not covering it.
        if file_row["is_test_file"] or file_row["is_generated"]:
            continue
        for symbol in store.list_symbols_for_file(path):
            if symbol["kind"] not in _TESTABLE_KINDS:
                continue
            if symbol["name"] in _ENTRY_POINT_NAMES:
                continue
            if not include_private and _is_private(symbol["name"]):
                continue
            # A three-line accessor does not earn its own test, and flagging
            # every one of them buries the findings that matter.
            if (symbol["loc"] or 0) < min_loc:
                continue
            scanned += 1
            if symbol["symbol_id"] in covered:
                tested += 1
                continue
            candidates.append(
                {
                    "symbol_id": symbol["symbol_id"],
                    "qualified_name": symbol["qualified_name"] or symbol["name"],
                    "kind": symbol["kind"],
                    "file": path,
                    "line": symbol["start_line"],
                    "end_line": symbol["end_line"],
                    "loc": symbol["loc"],
                    "complexity": symbol["cyclomatic_complexity"],
                    "language": file_row["language"],
                }
            )

    # Worst first: a long, branchy function with no test is where an
    # untested change does the most damage.
    candidates.sort(
        key=lambda row: (-(row["complexity"] or 0), -row["loc"], row["file"], row["line"])
    )
    return {
        "resolution": "exact",
        "languages": languages_in_scope,
        "symbols_scanned": scanned,
        "symbols_tested": tested,
        "coverage_ratio": (tested / scanned) if scanned else 0.0,
        "candidates_total": len(candidates),
        "candidates": candidates[:limit],
        "caveats": [
            "virtual dispatch resolves to the base declaration, not the override",
            "a test living outside the workspace is invisible to the index",
            "a thin delegator can be pinned by the test of what it delegates to",
            "reaching a symbol from a test is not the same as asserting on it",
        ],
    }
