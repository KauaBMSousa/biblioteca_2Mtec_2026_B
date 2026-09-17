"""The hash-check-then-reuse-or-reparse incremental indexing flow.

For every discovered file: hash it; if its `content_hash` + `parser_version`
+ `config_hash` match the stored row, reuse the stored symbols/diagnostics
untouched (no reparse, no rule recompute); otherwise reparse via the
matching adapter, recompute everything for that file, and replace its rows
in one transaction. Deleted files (present in the index but no longer
discovered) have their rows removed (cascading to symbols/diagnostics/
imports/symbol_extra).

Two genuinely project-wide passes — call-graph resolution
(`core/dependencies/callgraph.py`) and cross-file duplication detection
(`core/rules/duplication.py`) — run on every `index()` call over the whole
project's *current* symbol data. For changed files that data comes from
the fresh reparse; for unchanged files it's reconstructed from the index's
`symbol_extra` auxiliary table (token streams + reference identifiers)
without touching the filesystem or the parser again. This keeps parsing
itself properly incremental (the expensive step) while keeping these two
passes simple and always-correct — see `schema.py`'s module docstring for
the full rationale, and the Phase A final report for why this is a
documented simplification of the plan's §19 minimal-affected-set design
rather than a fully targeted incremental recompute.
"""

import hashlib
import importlib.metadata
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_intelligence.core.analysis import cppcheck as cppcheck_analysis
from code_intelligence.core.cache.invalidation import is_fresh
from code_intelligence.core.config import compute_config_hash
from code_intelligence.core.dependencies.callgraph import resolve as resolve_callgraph
from code_intelligence.core.diagnostics.violation import Violation
from code_intelligence.core.filesystem.discovery import discover_all
from code_intelligence.core.hashes.ast_hash import compute_ast_hash
from code_intelligence.core.index.store import IndexStore
from code_intelligence.core.parser import find_adapter
from code_intelligence.core.parser.ir import ClassInfo, FileAnalysis, FunctionInfo, IdentifierRef
from code_intelligence.core.rules import generated, run_file_rules
from code_intelligence.core.rules import untested
from code_intelligence.core.semantic import coverage as semantic_coverage
from code_intelligence.core.rules.duplication import check as check_duplication
from code_intelligence.core.symbols.ids import assign_symbol_ids, find_symbol_id_for_line, flatten_symbols

#: Bumped whenever adapter-parsing/kind-labeling logic changes in a way
#: that must invalidate every stored row even though a file's own content
#: didn't change.
#: 3: identifiers are classified by ROLE -- a name being declared vs one
#: merely used. Bumping this reparses every file, which is required:
#: parameter counts feed symbol ids, and they were counting type names.
ADAPTER_LOGIC_VERSION = "3"

_GRAMMAR_PACKAGES = ("tree-sitter", "tree-sitter-cpp", "tree-sitter-java", "tree-sitter-php", "tree-sitter-javascript")

#: `SymbolRecord.kind` values that represent a type definition (as opposed
#: to a function/method/constructor/destructor) — used to reconstruct a
#: lite `FileAnalysis` from stored rows for an unchanged (reused) file.
_CLASS_KINDS = {"class", "struct", "interface", "enum", "record", "trait"}

#: Reference-identifier kinds `core/dependencies/callgraph.py` reads.
_CALL_LIKE_KINDS = {"function", "class"}


def compute_parser_version() -> str:
    """Combine the adapter-logic version with installed tree-sitter grammar package versions.

    A mismatch against a file's stored `parser_version` is treated as a
    cache miss even when its `content_hash` is unchanged (an
    adapter-logic change or a grammar package upgrade invalidates every
    cached entry).
    """
    parts = [ADAPTER_LOGIC_VERSION]
    for package in _GRAMMAR_PACKAGES:
        try:
            parts.append(importlib.metadata.version(package))
        except importlib.metadata.PackageNotFoundError:
            parts.append("?")
    return "|".join(parts)


@dataclass(slots=True)
class IndexStats:
    """Summary of one `index()` run, returned to the CLI/`Workspace` caller."""

    files_total: int
    files_reused: int
    files_reparsed: int
    files_removed: int
    symbols_total: int
    diagnostics_total: int
    duration_seconds: float
    #: Paths reparsed in this run. The semantic layer needs the SET, not the
    #: count: a changed header invalidates every translation unit that read
    #: it, and that is computed from these paths.
    reparsed_paths: frozenset[str] = frozenset()
    #: The semantic refresh report, when `index(semantic_refresh=True)` ran.
    #: None means the exact edges were NOT brought up to date in this pass --
    #: which is not the same as "there was nothing to do".
    semantic: dict | None = None


def _read_source(path: Path) -> str | None:
    """Read a file's text; None (not raise) on any I/O error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _content_hash(source: str) -> str:
    """The same blake2b hex digest the old tool used as its whole-file `file_hash`."""
    return hashlib.blake2b(source.encode("utf-8", errors="replace")).hexdigest()


def _all_functions(analysis: FileAnalysis) -> list[FunctionInfo]:
    """Every function/method in one file, top-level and class-owned."""
    functions = list(analysis.top_level_functions)
    for cls in analysis.classes:
        functions.extend(cls.methods)
    return functions


def _reference_identifiers(func: FunctionInfo) -> list[dict[str, Any]]:
    """The call/instantiation-like identifiers `core/dependencies` needs, as plain dicts."""
    return [
        {"name": ident.name, "kind": ident.kind, "line": ident.line, "column": ident.column}
        for ident in func.identifiers
        if ident.kind in _CALL_LIKE_KINDS
    ]


def _violation_row(violation: Violation) -> dict[str, Any]:
    """Serialize one Violation into a `diagnostics` table row dict."""
    return {
        "symbol_id": violation.symbol_id,
        "file_path": violation.file,
        "rule": violation.code,
        "severity": int(violation.severity),
        "confidence": violation.confidence,
        "start_line": violation.line,
        "end_line": violation.end_line,
        "start_column": violation.column,
        "end_column": violation.end_column,
        "value": None,
        "threshold": None,
        "message": violation.message,
        "detail_json": json.dumps(violation.detail) if violation.detail is not None else None,
    }


class Indexer:
    """Runs the incremental index/reuse/reparse flow for one workspace root."""

    def __init__(self, root: Path, store: IndexStore, config: dict) -> None:
        """Bind an indexer to an already-open `store` and effective `config`."""
        self.root = root
        self.store = store
        self.config = config

    def run(self) -> IndexStats:
        """Run one full incremental index pass; returns run statistics."""
        started = time.monotonic()
        parser_version = compute_parser_version()
        config_hash = compute_config_hash(self.config)

        discovery = discover_all(self.root, self.config)
        discovered_relpaths = {str(path.relative_to(self.root)) for path in discovery.files}
        existing_relpaths = set(self.store.list_file_paths())

        removed = existing_relpaths - discovered_relpaths
        reused = 0
        reparsed = 0
        fresh_analyses: dict[str, FileAnalysis] = {}

        with self.store.transaction():
            for relpath in sorted(removed):
                self.store.delete_file(relpath)

            for path in discovery.files:
                relpath = str(path.relative_to(self.root))
                source = _read_source(path)
                if source is None:
                    continue
                content_hash = _content_hash(source)
                existing = self.store.get_file(relpath)

                if is_fresh(existing, content_hash, parser_version, config_hash):
                    reused += 1
                    continue

                adapter = find_adapter(path)
                if adapter is None:
                    continue

                analysis = adapter.analyze(path, source)
                analysis.relpath = relpath
                generated.annotate(analysis, source, self.config)
                assign_symbol_ids(analysis)
                analysis.ast_hash = compute_ast_hash(analysis)
                violations = run_file_rules(analysis, self.config)
                symbols = flatten_symbols(analysis, source)

                self.store.delete_symbols_for_file(relpath)
                self.store.delete_diagnostics_for_file(relpath)
                self.store.delete_imports_for_file(relpath)
                self.store.upsert_file(
                    {
                        "path": relpath,
                        "language": analysis.language,
                        "content_hash": content_hash,
                        "ast_hash": analysis.ast_hash,
                        "physical_lines": analysis.physical_lines,
                        "logical_code_lines": analysis.logical_code_lines,
                        "comment_lines": analysis.comment_lines,
                        "blank_lines": analysis.blank_lines,
                        "parse_ok": int(analysis.parse_ok),
                        "parse_fallback_used": int(analysis.parse_fallback_used),
                        "parse_error": analysis.parse_error,
                        "parse_confidence": analysis.parse_confidence,
                        "is_generated": int(analysis.is_generated),
                        "is_test_file": int(analysis.is_test_file),
                        "is_config_file": int(analysis.is_config_file),
                        "parser_version": parser_version,
                        "config_hash": config_hash,
                        "indexed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    }
                )

                by_id = {symbol.symbol_id: symbol for symbol in symbols}
                for symbol in symbols:
                    self.store.insert_symbol(
                        {
                            "symbol_id": symbol.symbol_id,
                            "file_path": relpath,
                            "kind": symbol.kind,
                            "name": symbol.name,
                            "qualified_name": symbol.qualified_name,
                            "namespace": symbol.namespace,
                            "start_line": symbol.start_line,
                            "start_column": symbol.start_column,
                            "end_line": symbol.end_line,
                            "end_column": symbol.end_column,
                            "body_start_line": symbol.body_start_line,
                            "body_end_line": symbol.body_end_line,
                            "loc": symbol.loc,
                            "cyclomatic_complexity": symbol.cyclomatic_complexity,
                            "has_doc": int(symbol.has_doc),
                            "content_hash": symbol.content_hash,
                            "confidence": symbol.confidence,
                        }
                    )
                for func in _all_functions(analysis):
                    if func.symbol_id in by_id:
                        self.store.upsert_symbol_extra(
                            func.symbol_id, func.token_stream, _reference_identifiers(func)
                        )
                for cls in analysis.classes:
                    if cls.symbol_id in by_id:
                        self.store.upsert_symbol_extra(cls.symbol_id, [], [])

                for violation in violations:
                    violation.symbol_id = find_symbol_id_for_line(analysis, violation.line)
                    self.store.insert_diagnostic(_violation_row(violation))

                for imp in analysis.imports:
                    self.store.insert_import(relpath, imp.module, imp.line)

                fresh_analyses[relpath] = analysis
                reparsed += 1

            # -- project-wide passes: call-graph + duplication -----------
            live_relpaths = discovered_relpaths - removed
            all_results: list[tuple[FileAnalysis, list]] = []
            for relpath in sorted(live_relpaths):
                if relpath in fresh_analyses:
                    all_results.append((fresh_analyses[relpath], []))
                    continue
                file_row = self.store.get_file(relpath)
                if file_row is None:
                    continue
                all_results.append((self._reconstruct_lite_analysis(file_row), []))

            resolve_callgraph(all_results)

            self.store.delete_all_dependencies()
            for analysis, _violations in all_results:
                for func in _all_functions(analysis):
                    for callee_id in func.calls:
                        self.store.insert_dependency(func.symbol_id, callee_id, "call", func.call_graph_confidence)
                    for ref_id in func.references:
                        self.store.insert_dependency(
                            func.symbol_id, ref_id, "reference", func.call_graph_confidence
                        )

            analyses_only = [analysis for analysis, _violations in all_results]
            duplicate_violations = check_duplication(analyses_only, self.config)
            self.store.delete_diagnostics_by_rule("DUPLICATE_BLOCK")
            analyses_by_relpath = {analysis.relpath: analysis for analysis in analyses_only}
            for violation in duplicate_violations:
                owner_analysis = analyses_by_relpath.get(violation.file)
                if owner_analysis is not None:
                    violation.symbol_id = find_symbol_id_for_line(owner_analysis, violation.line)
                self.store.insert_diagnostic(_violation_row(violation))

            # -- project-wide pass: test coverage ------------------------
            # Runs last because it reads the semantic index, which the passes
            # above do not touch. It is best-effort by design: a workspace
            # with no semantic backend still indexes fine, it just cannot
            # answer this question, and check() says so rather than staying
            # silent.
            self.store.delete_diagnostics_by_rule(untested.UNTESTED_CODE)
            self.store.delete_diagnostics_by_rule(untested.UNKNOWN_CODE)
            pending_units = semantic_coverage.pending_units(self.store, self.root)
            for violation in untested.check(self.store, pending_units, self.config):
                self.store.insert_diagnostic(_violation_row(violation))

            # -- project-wide pass: C++ static analysis (cppcheck) -------
            # Runs only when a C++ file actually moved this pass (or the
            # cache holds no cppcheck rows yet). A `--cppcheck-build-dir`
            # cache makes the whole-fileset re-run cheap: only changed
            # files are re-analysed. Skipped silently when cppcheck is not
            # installed or the workspace has no C++.
            self._run_cppcheck(fresh_analyses, removed)

            # Revision bookkeeping (ENHANCEME §O). A pass that reparsed or
            # dropped a file moved the code on disk since `content` was last
            # bumped by an edit; either way `indexed` is now caught up.
            if reparsed or removed:
                self.store.bump_revision("content")
            self.store.set_revision("indexed", self.store.get_revision("content"))

            self.store.meta_set("parser_version", parser_version)
            self.store.meta_set("config_hash", config_hash)
            self.store.meta_set("index_version", ADAPTER_LOGIC_VERSION)
            self.store.meta_set("last_indexed_at", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))

        return IndexStats(
            files_total=len(live_relpaths),
            files_reused=reused,
            files_reparsed=reparsed,
            reparsed_paths=frozenset(fresh_analyses),
            files_removed=len(removed),
            symbols_total=len(self.store.list_all_symbols()),
            diagnostics_total=len(self.store.list_diagnostics()),
            duration_seconds=time.monotonic() - started,
        )

    def _run_cppcheck(self, fresh_analyses: dict[str, FileAnalysis], removed: set[str]) -> None:
        """Re-run cppcheck when a C++ file moved this pass; refresh the `CPPCHECK_*` rows.

        Never raises: an analyzer that crashes must not fail the index. The
        pass is a no-op when cppcheck is unavailable, disabled, or the
        workspace has no C++ — and skipped (rows left as-is) when no C++
        file changed and the cache already holds cppcheck findings.
        """
        cpp_suffixes = tuple(self.config.get("languages", {}).get("cpp", []))
        cpp_reparsed = any(a.language == "cpp" for a in fresh_analyses.values())
        cpp_removed = any(r.endswith(cpp_suffixes) for r in removed) if cpp_suffixes else False
        already_ran = self.store.has_diagnostics_with_rule_prefix(cppcheck_analysis.RULE_PREFIX)

        if not (cpp_reparsed or cpp_removed or not already_ran):
            return

        try:
            violations = cppcheck_analysis.check(
                self.store,
                self.root,
                self.config,
                build_dir=self.root / ".code-intelligence" / "cppcheck",
            )
        except Exception:  # noqa: BLE001 - a flaky analyzer never fails the index
            return
        if violations is None:
            return

        self.store.delete_diagnostics_by_rule_prefix(cppcheck_analysis.RULE_PREFIX)
        for violation in violations:
            self.store.insert_diagnostic(_violation_row(violation))

    def _reconstruct_lite_analysis(self, file_row: dict[str, Any]) -> FileAnalysis:
        """Rebuild a lite `FileAnalysis` for an unchanged file from stored rows.

        Sufficient for the two project-wide passes (call-graph, duplication)
        without touching the filesystem or the parser: real `symbol_id`s,
        real token streams and reference identifiers (from `symbol_extra`),
        real line spans — only `nesting_depth`/some cosmetic IR fields are
        left at defaults, since neither pass reads them.
        """
        relpath = file_row["path"]
        symbol_rows = self.store.list_symbols_for_file(relpath)
        classes_by_name: dict[str, ClassInfo] = {}
        top_level_functions: list[FunctionInfo] = []

        for row in symbol_rows:
            if row["kind"] in _CLASS_KINDS:
                classes_by_name[row["name"]] = ClassInfo(
                    name=row["name"],
                    kind=row["kind"],
                    start_line=row["start_line"],
                    end_line=row["end_line"],
                    has_doc=bool(row["has_doc"]),
                    start_column=row["start_column"],
                    end_column=row["end_column"],
                    symbol_id=row["symbol_id"],
                    namespace=row["namespace"],
                    kind_confidence=row["confidence"],
                )

        for row in symbol_rows:
            if row["kind"] in _CLASS_KINDS:
                continue
            extra = self.store.get_symbol_extra(row["symbol_id"]) or {"token_stream": [], "references": []}
            identifiers = [
                IdentifierRef(name=ref["name"], kind=ref["kind"], line=ref.get("line", 0), column=ref.get("column", 1))
                for ref in extra["references"]
            ]
            owner_class = None
            if "." in row["qualified_name"]:
                candidate = row["qualified_name"].rsplit(".", 1)[0]
                if candidate in classes_by_name:
                    owner_class = candidate
            func = FunctionInfo(
                name=row["name"],
                qualified_name=row["qualified_name"],
                start_line=row["start_line"],
                end_line=row["end_line"],
                physical_lines=row["loc"],
                has_doc=bool(row["has_doc"]),
                nesting_depth=0,
                cyclomatic_complexity=row["cyclomatic_complexity"] or 0,
                identifiers=identifiers,
                token_stream=extra["token_stream"],
                is_method=owner_class is not None,
                owner_class=owner_class,
                start_column=row["start_column"],
                end_column=row["end_column"],
                body_start_line=row["body_start_line"],
                body_end_line=row["body_end_line"],
                symbol_id=row["symbol_id"],
                namespace=row["namespace"],
                kind=row["kind"],
                kind_confidence=row["confidence"],
            )
            if owner_class is not None:
                classes_by_name[owner_class].methods.append(func)
            else:
                top_level_functions.append(func)

        return FileAnalysis(
            path=str(self.root / relpath),
            relpath=relpath,
            language=file_row["language"],
            physical_lines=file_row["physical_lines"],
            logical_code_lines=file_row["logical_code_lines"],
            comment_lines=file_row["comment_lines"],
            blank_lines=file_row["blank_lines"],
            classes=list(classes_by_name.values()),
            top_level_functions=top_level_functions,
            imports=[],
            parse_ok=bool(file_row["parse_ok"]),
            parse_fallback_used=bool(file_row["parse_fallback_used"]),
            parse_error=file_row["parse_error"],
            is_generated=bool(file_row["is_generated"]),
            is_test_file=bool(file_row["is_test_file"]),
            is_config_file=bool(file_row["is_config_file"]),
            file_hash=file_row["content_hash"],
            parse_confidence=file_row["parse_confidence"],
            ast_hash=file_row["ast_hash"],
        )


__all__ = ["Indexer", "IndexStats", "compute_parser_version"]
