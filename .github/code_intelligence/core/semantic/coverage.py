"""How much of the exact index is actually built, per language.

Two callers need this number and neither may import the other: `Workspace`
answers coverage questions with it, and `Indexer` gates the test-coverage
rule on it. It lives here because it is neither -- it is a query over
`semantic_units` joined with what each backend claims it should have
extracted.

The number matters because an empty exact answer is ambiguous. "Nothing
calls this" from a repo where 5 of 300 translation units have been
extracted reads identically to the same sentence from a fully covered one,
and only one of them is evidence.
"""

from typing import Any

from code_intelligence.core.index.store import IndexStore


def pending_units(store: IndexStore, root: Any) -> dict[str, Any]:
    """Per language: units a backend claims, and how many are not done yet."""
    from code_intelligence.core.semantic.registry import BACKENDS

    # `ok = 1` on purpose. A unit that failed to parse is recorded, but its
    # calls were never seen -- for deciding what is unreferenced or untested
    # it is exactly as blind as a unit nobody extracted, and counting it as
    # covered is how a caller hidden in an unparseable file turns into a
    # deletion, or a tested function into an untested one.
    extracted = {
        row[0] for row in store.connection.execute("SELECT unit FROM semantic_units WHERE ok = 1")
    }
    failed_by_language = {
        row["language"]: row["n"]
        for row in store.connection.execute(
            "SELECT language, count(*) n FROM semantic_units WHERE ok = 0 GROUP BY language"
        )
    }
    pending: dict[str, Any] = {}
    for language, backend in BACKENDS.items():
        indexed = [row["path"] for row in store.list_files() if row["language"] == language]
        available, missing = backend.availability(root)
        if not available:
            pending[language] = {
                "available": False,
                "missing": missing,
                "indexed_files": len(indexed),
            }
            continue
        # What the backend actually claims: C++ headers are not units, so
        # counting every indexed file would overstate what is missing.
        claimed = backend.units_for(root, indexed)
        remaining = [unit for unit in claimed if unit not in extracted]
        pending[language] = {
            "available": True,
            "backend": backend.tool,
            "units_claimed": len(claimed),
            "units_pending": len(remaining),
            "units_failed": failed_by_language.get(language, 0),
            "covered_fraction": (round(1 - len(remaining) / len(claimed), 3) if claimed else 1.0),
        }
    return pending
