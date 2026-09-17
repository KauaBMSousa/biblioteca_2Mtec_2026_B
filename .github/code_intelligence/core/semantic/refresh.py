"""Keeping the exact edges true as the code changes.

Exact data that has gone stale is worse than heuristic data, because it
still looks authoritative. So the refresh rule is deliberately pessimistic:

* a unit whose own text changed is re-extracted;
* a unit that READ a changed file is re-extracted too -- editing a C++
  header does not touch the .cpp that includes it, but it can change what
  every call in that .cpp resolves to;
* a unit that could not be analyzed is recorded as failed, so "no edges"
  and "not analyzable" never look alike;
* a language with no available backend is simply not claimed: its questions
  keep being answered by name matching, and keep saying so.

Cost is real and worth stating: one C++ translation unit in this project
carries 1539 in-workspace includes and takes about a minute to parse. That
is why extraction is incremental and why `stale_units` computes the exact
set to redo instead of rebuilding everything.
"""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from code_intelligence.core.hashes.content_hash import content_hash
from code_intelligence.core.index import semantic_store
from code_intelligence.core.index.store import IndexStore
from code_intelligence.core.semantic.registry import BACKENDS


def _unit_hash(root: Path, unit: str) -> str:
    try:
        return content_hash((root / unit).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""


def refresh(
    store: IndexStore,
    root: Path,
    changed_files: set[str] | None = None,
    languages: list[str] | None = None,
    limit: int | None = None,
    batch_size: int | None = None,
    on_progress: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Bring the semantic edges back in step with the files on disk.

    `changed_files` is what the index just reparsed. Passing None means "no
    incremental information available" and re-extracts every unit the
    backends claim, which is the correct-but-slow path used for the first
    build.

    `batch_size` splits that slow path into chunks that are extracted,
    written and COMMITTED one at a time. Three things follow, and all three
    matter at C++ scale (300 translation units, roughly an hour):

    * the write lock is held for a moment per chunk instead of for the whole
      run, so the daemon can keep answering queries meanwhile;
    * a run that is killed halfway keeps what it finished -- the units it
      never reached are still "never extracted" and the next refresh picks
      up exactly those;
    * `on_progress` is called after each chunk, so an hour-long build is
      observable instead of silent.

    Without it the whole extraction is held in memory and written once.
    """
    report: dict[str, Any] = {
        "languages": {},
        "units_extracted": 0,
        "units_failed": 0,
        "edges": 0,
        "skipped_languages": {},
    }

    for language, backend in BACKENDS.items():
        if languages and language not in languages:
            continue
        available, missing = backend.availability(root)
        if not available:
            # Not an error: the language simply keeps its heuristic answers,
            # and the reason is reported rather than swallowed.
            report["skipped_languages"][language] = missing
            continue

        not_source = _embedded_listings(store)
        # Excluding them from future extraction is not enough on its own: a
        # unit that failed BEFORE this exclusion existed keeps its ok = 0 row,
        # and coverage counts failures whether or not the unit is still a
        # candidate. Without this purge the refusal outlives its cause -- the
        # file is never retried, so the failure can never clear.
        _purge_units(store, not_source)
        candidates = [
            row["path"]
            for row in store.list_files()
            if row["language"] == language and row["path"] not in not_source
        ]
        if changed_files is None:
            units = candidates
        else:
            stale = semantic_store.stale_units(store, changed_files)
            never_extracted = {
                path
                for path in candidates
                if store.connection.execute(
                    "SELECT 1 FROM semantic_units WHERE unit = ?", (path,)
                ).fetchone()
                is None
            }
            units = sorted((stale & set(candidates)) | never_extracted)

        # Let the backend drop what is not a unit of its own (C++ headers),
        # so coverage counts what was actually attempted.
        units = backend.units_for(root, units)
        if limit is not None:
            units = units[:limit]
        if not units:
            report["languages"][language] = {"backend": backend.tool, "units": 0, "edges": 0}
            continue

        started = time.time()
        indexed = _workspace_files(store)
        chunks = (
            [units[i : i + batch_size] for i in range(0, len(units), batch_size)]
            if batch_size
            else [units]
        )
        totals = {"units": 0, "units_ok": 0, "edges": 0, "batches_failed": []}
        for number, chunk in enumerate(chunks, start=1):
            outcome = _extract_batch(store, root, backend, language, chunk, indexed)
            if outcome is None:
                totals["batches_failed"].append(number)
            else:
                totals["units"] += len(chunk)
                totals["units_ok"] += outcome["units_ok"]
                totals["edges"] += outcome["edges"]
            if on_progress is not None:
                on_progress(
                    {
                        "language": language,
                        "batch": number,
                        "batches": len(chunks),
                        "units_done": totals["units"],
                        "units_total": len(units),
                        "edges": totals["edges"],
                        "seconds": round(time.time() - started, 1),
                    }
                )

        report["languages"][language] = {
            "backend": backend.tool,
            "units": totals["units"],
            "units_ok": totals["units_ok"],
            "units_failed": totals["units"] - totals["units_ok"],
            "edges": totals["edges"],
            "seconds": round(time.time() - started, 1),
        }
        if totals["batches_failed"]:
            # Not swallowed: those units stay unextracted and get retried,
            # but the caller has to be told the coverage is short.
            report["languages"][language]["batches_failed"] = totals["batches_failed"]
        report["units_extracted"] += totals["units_ok"]
        report["units_failed"] += totals["units"] - totals["units_ok"]
        report["edges"] += totals["edges"]

    store.connection.commit()
    return report


def _embedded_listings(store: IndexStore) -> set[str]:
    """Files with a source extension whose content is a document listing.

    A `.py` whose first line is ``\\begin{lstlisting}[language=Python]`` is a
    LaTeX fragment a paper ``\\input``s. It has never parsed as Python and
    never will, so handing it to a semantic backend produces a permanent
    failed unit -- and a failed unit is not a passing annoyance. Every
    coverage-gated answer refuses while one exists, by design, because a unit
    that could not be parsed hides its calls just as completely as one that
    was never extracted. Three LaTeX listings in this project's
    `documentation/` were therefore enough to make `unreferenced_symbols` and
    `untested_symbols` refuse forever, with a message telling the reader to
    run an extraction that could never succeed.

    The fact is already known and already stored: `rules/embedded_listing`
    records it as a NOT_SOURCE_FILE diagnostic. Reading it back from there
    keeps this fix free of a schema migration, which on this project would
    mean rebuilding an index that takes about an hour of clang time.
    """
    return {
        row["file_path"]
        for row in store.connection.execute(
            "SELECT DISTINCT file_path FROM diagnostics WHERE rule = 'NOT_SOURCE_FILE'"
        )
    }


def _purge_units(store: IndexStore, units: set[str]) -> None:
    """Forget extraction records for paths that are no longer units."""
    if not units:
        return
    placeholders = ", ".join("?" for _ in units)
    store.connection.execute(
        f"DELETE FROM semantic_units WHERE unit IN ({placeholders})", tuple(sorted(units))
    )


def _workspace_files(store: IndexStore) -> set[str]:
    """Every path the structural index contains -- the workspace's own code.

    This is the line between the project and its dependencies, and it is
    already drawn: the indexer applies `exclude_globs` plus .gitignore, so
    `out/build/.../_deps/`, `node_modules/`, `vendor/` and friends are
    absent from it. The semantic backends do not consult that config -- they
    ask a compiler, and a compiler happily resolves an include into
    googletest -- so the filter has to be applied on the way in.
    """
    return {row["path"] for row in store.list_files()}


def _extract_batch(
    store: IndexStore, root: Path, backend, language: str, units: list[str], indexed: set[str]
) -> dict[str, Any] | None:
    """Extract and commit one chunk of units. None when the backend threw.

    `forget_units` runs first on purpose. If extraction then fails, these
    units are left recorded as never-extracted rather than holding stale
    edges that would still look authoritative -- and the next refresh will
    select exactly them.
    """
    semantic_store.forget_units(store, set(units))
    try:
        extraction = backend.extract(root, units)
    except Exception:  # a backend that breaks must not break the index
        store.connection.commit()
        return None

    edges_by_unit: dict[str, list[dict[str, Any]]] = {unit: [] for unit in units}
    for edge in extraction.edges:
        # An edge into third-party code is unusable and enormous: one C++
        # translation unit resolves calls into googletest, xtensor and libc++,
        # and 58% of the edges on this project pointed there. None of them can
        # be answered -- `find_references` maps an edge back to an indexed
        # symbol, and there is no indexed symbol in a dependency. Edges whose
        # target the compiler could not resolve at all (`to_file is None`) are
        # kept: their name still serves the heuristic path.
        if edge.to_file is not None and edge.to_file not in indexed:
            continue
        edges_by_unit.setdefault(edge.from_file, []).append(
            {
                "from_file": edge.from_file,
                "from_line": edge.from_line,
                "from_column": edge.from_column,
                "to_name": edge.to_name,
                "to_file": edge.to_file,
                "to_line": edge.to_line,
                "to_usr": edge.to_usr,
                "kind": edge.kind,
            }
        )

    definitions_by_unit: dict[str, list[tuple[str, str, int, str]]] = {}
    for usr, def_file, def_line, name in extraction.definitions:
        definitions_by_unit.setdefault(def_file, []).append((usr, def_file, def_line, name))

    for unit in units:
        failure = extraction.failures.get(unit)
        semantic_store.replace_unit(
            store,
            unit=unit,
            language=language,
            backend=backend.tool,
            unit_hash=_unit_hash(root, unit),
            edges=edges_by_unit.get(unit, []),
            depends_on=[
                dependency
                for dependency in extraction.dependencies.get(unit, [])
                if dependency in indexed
            ],
            ok=failure is None,
            error=failure,
            definitions=definitions_by_unit.get(unit, []),
        )
    store.connection.commit()
    return {
        "units_ok": sum(1 for unit in units if unit not in extraction.failures),
        "edges": len(extraction.edges),
    }
