"""`Workspace`: the public API tying together index/parser/symbols/rules/context/git.

```python
workspace = Workspace.open("/repo")
workspace.index()
violations = workspace.get_violations(severity="critical")
symbol = workspace.get_symbol("cpp:src/tensor/Tensor.cpp:method:Tensor::backward")
context = workspace.get_context(symbol_id=symbol["symbol_id"], max_lines=180)
```

The literal API from the plan's "Workspace & identity" section. All state
(the SQLite index; in Phase B, the daemon socket/pidfile) lives in
`<root>/.code-intelligence/`.
"""

from pathlib import Path
from typing import Any

from code_intelligence.core import context as context_module
from code_intelligence.core.config import load_config
from code_intelligence.core import exec as exec_module
from code_intelligence.core.git import blame as git_blame_module
from code_intelligence.core.git import log as git_log_module
from code_intelligence.core.git import status as git_status_module
from code_intelligence.core.git.diff_report import (
    STAGED,
    WORKING_TREE,
    changed_files_for,
    content_at,
    diff_stat_for,
    symbol_diff,
    violation_diff,
)
from code_intelligence.core.filesystem.markdown_links import check_markdown_links
from code_intelligence.core.hashes.content_hash import content_hash
from code_intelligence.core.index.indexer import Indexer, IndexStats
from code_intelligence.core import semantic
from code_intelligence.core.semantic import coverage as semantic_coverage_module
from code_intelligence.core.context import deadcode, editing, inspection, searching, structural, testcoverage
from code_intelligence.core.context import impact as impact_module
from code_intelligence.core.context.snapshot import workspace_snapshot as _workspace_snapshot
from code_intelligence.core.context import transaction as _transaction
from code_intelligence.core.context import transform as _transform
from code_intelligence.core.context import refactor as _refactor
from code_intelligence.core.context import code_actions as _code_actions
from code_intelligence.core.context import recipes as _recipes
from code_intelligence.core.context import py_refactor2 as _py_refactor2
from code_intelligence.core.context import planner as _planner
from code_intelligence.core.context.inspect import inspect as _inspect
from code_intelligence.core.context import cpp_includes as _cpp_includes
from code_intelligence.core.context import cpp_macros as _cpp_macros
from code_intelligence.core.context import cpp_transform as _cpp_transform
from code_intelligence.core.semantic import cpp_queries as _cpp_queries
from code_intelligence.core.cache.answers import cached as _cached
from code_intelligence.core.exec.diagnose import diagnose as _diagnose
from code_intelligence.core.workspace.revisions import revisions as _revisions
from code_intelligence.core.index import semantic_store
from code_intelligence.core.index.store import IndexStore
from code_intelligence.core.parser import find_adapter
from code_intelligence.core.rules import generated, run_file_rules
from code_intelligence.core.symbols.ids import assign_symbol_ids, find_symbol_id_for_line, flatten_symbols
from code_intelligence.core.workspace import identity
from code_intelligence.core.workspace.security import resolve_within_workspace


class Workspace:
    """One open workspace: its root, effective config, and index store."""

    def __init__(self, root: Path, config: dict, store: IndexStore, workspace_id: str, first_use: bool) -> None:
        """Bind an already-open `store` to a resolved `root` — use `Workspace.open()` instead."""
        self.root = root
        self.config = config
        self.store = store
        self.workspace_id = workspace_id
        self._first_use = first_use

    @classmethod
    def open(cls, root: str | Path) -> "Workspace":
        """Open (or create) a workspace at `root`.

        Detects, before touching anything, whether `<root>/.code-intelligence/`
        already existed — if not, this is a first-ever open, and the first
        successful `index()` call triggers `bootstrap/skill_writer.py`.
        """
        resolved = identity.resolve_root(root)
        first_use = not identity.state_dir(resolved).exists()
        config = load_config(None, resolved)
        store = IndexStore(identity.index_db_path(resolved))
        store.open()
        workspace_id = identity.compute_workspace_id(resolved)
        store.meta_set("workspace_id", workspace_id)
        return cls(resolved, config, store, workspace_id, first_use)

    @property
    def first_use(self) -> bool:
        """True until this workspace's first successful `index()` call has run bootstrap."""
        return self._first_use

    def close(self) -> None:
        """Close the underlying index store."""
        self.store.close()

    def __enter__(self) -> "Workspace":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- indexing -------------------------------------------------------

    def index(
        self, semantic_refresh: bool = False, batch_size: int | None = 8
    ) -> IndexStats:
        """Run one incremental index pass; triggers bootstrap on first-ever use.

        `semantic_refresh` additionally brings the exact (type-resolved)
        edges back in step with whatever was reparsed. It is off by default
        because it is genuinely expensive -- one C++ translation unit in a
        real project carries 1539 in-workspace includes and takes about a
        minute -- and because a structural index is useful on its own. When
        it IS on, only the stale units are redone, computed from what this
        pass reparsed plus what those files are read by.
        """
        stats = Indexer(self.root, self.store, self.config).run()
        if self._first_use:
            from code_intelligence.bootstrap.skill_writer import bootstrap_workspace

            bootstrap_workspace(self.root)
            self._first_use = False
        if semantic_refresh:
            # Kept on the stats object rather than returned separately: a
            # caller that reindexes and forgets to look at the semantic
            # result is exactly how exact edges go quietly stale.
            stats.semantic = self.refresh_semantic(
                changed_files=set(stats.reparsed_paths), batch_size=batch_size
            )
        return stats

    def status(self) -> dict[str, Any]:
        """Workspace identity + index metadata + summary counts."""
        files = self.store.list_files()
        diagnostics = self.store.list_diagnostics()
        return {
            "workspace_id": self.workspace_id,
            "root": str(self.root),
            "schema_version": self.store.meta_get("schema_version"),
            "parser_version": self.store.meta_get("parser_version"),
            "config_hash": self.store.meta_get("config_hash"),
            "last_indexed_at": self.store.meta_get("last_indexed_at"),
            "files_indexed": len(files),
            "symbols_indexed": len(self.store.list_all_symbols()),
            "diagnostics_count": len(diagnostics),
        }

    # -- context hierarchy (L1-L6) + budget -----------------------------

    def get_workspace_structure(self) -> dict[str, Any]:
        """L1: per-file summary of the whole workspace."""
        return context_module.get_workspace_structure(self.store)

    def get_file_structure(self, path: str) -> dict[str, Any] | None:
        """L2: every symbol in one file, no source content."""
        return context_module.get_file_structure(self.store, path)

    def get_symbol(self, symbol_id: str) -> dict[str, Any] | None:
        """L3: one symbol's full metadata, no source content."""
        return context_module.get_symbol(self.store, symbol_id)

    def find_symbol(self, value: str, file: str | None = None) -> dict[str, Any] | None:
        """Resolve a symbol_id, or a bare/qualified name scoped to `file`, to its L3 payload."""
        row = self.store.get_symbol(value)
        if row is None:
            row = context_module.find_symbol_by_name(self.store, value, file)
        if row is None:
            return None
        return context_module.symbol_to_dict(self.store, row)

    def get_context(self, symbol_id: str, max_lines: int | None = None, max_bytes: int | None = None) -> dict[str, Any] | None:
        """L4/5: one symbol's exact source content, honoring the Context Budget."""
        if max_lines is None and max_bytes is None:
            max_lines = self.config.get("context_budget", {}).get("default_max_lines")
        return context_module.get_context(self.store, self.root, symbol_id, max_lines, max_bytes)

    def search_symbols(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Indexed substring search over every symbol's name/qualified_name (backs `workspace/symbol`)."""
        return context_module.search_symbols(self.store, query, limit)

    def find_dependents(self, symbol_id: str) -> list[dict[str, Any]]:
        """L6: every symbol that calls/references `symbol_id`."""
        return _cached(
            self,
            "find_dependents",
            {"symbol_id": symbol_id},
            lambda: context_module.find_dependents(self.store, symbol_id),
        )

    def find_dependencies(self, symbol_id: str) -> list[dict[str, Any]]:
        """L6: every symbol `symbol_id` calls/references."""
        return context_module.find_dependencies(self.store, symbol_id)

    def get_agent_context(self) -> dict[str, Any]:
        """The minimal, token-economical `{critical_violations, recommended_context}` payload."""
        return context_module.get_agent_context(self.store, self.config)

    # -- agent-facing composite layer (ENHANCEME §Z) ---------------------

    def revisions(self) -> dict[str, Any]:
        """The `{content, indexed, semantic, git, index_fresh}` revision token block."""
        return _revisions(self.store, self.root)

    def workspace_snapshot(self) -> dict[str, Any]:
        """L0: one compact state payload (index freshness, git, coverage, attention). No source."""
        return _workspace_snapshot(self.store, self.config, self.root)

    def impact(self, symbol_id: str, max_depth: int = 5, limit: int = 50) -> dict[str, Any]:
        """Transitive reverse-dependency impact of changing `symbol_id` -- counts first."""
        return _cached(
            self,
            "impact",
            {"symbol_id": symbol_id, "max_depth": max_depth, "limit": limit},
            lambda: impact_module.impact(self.store, symbol_id, max_depth, limit),
        )

    def affected_tests(self, symbol_id: str, max_depth: int = 5) -> dict[str, Any]:
        """The test files that transitively reach `symbol_id`, plus a `run_tests` filter."""
        return _cached(
            self,
            "affected_tests",
            {"symbol_id": symbol_id, "max_depth": max_depth},
            lambda: impact_module.affected_tests(self.store, symbol_id, max_depth),
        )

    def diagnose(
        self,
        scope: str = "all",
        toolchain: str | None = None,
        filter: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run the build/tests and return only localized failure facts (no raw logs)."""
        return _diagnose(
            self.root, self.store, scope, toolchain=toolchain, filter=filter, timeout=timeout
        )

    def cppcheck(self, files: list[str] | None = None) -> dict[str, Any]:
        """Run Cppcheck on demand over `files` (or every indexed C++ file), counts first.

        The same analyzer the index runs automatically — call this to
        re-check specific files after an edit without a full reindex.
        Returns `{available, tool_version, files_analyzed, finding_count,
        by_severity, findings}`; each finding is `{code, severity, file,
        line, symbol, message, confidence}` — no source.
        """
        from code_intelligence.core.analysis import cppcheck as _cppcheck

        available, _version = _cppcheck.available()
        if not available:
            return {"available": False, "reason": "cppcheck is not installed", "findings": []}

        if files is None:
            files = sorted(
                row["path"] for row in self.store.list_files() if row["language"] == "cpp"
            )
        result = _cppcheck.analyze(
            self.root, files, self.config,
            build_dir=self.root / ".code-intelligence" / "cppcheck",
        )
        if result.error:
            return {
                "available": True, "tool_version": result.tool_version,
                "error": result.error, "findings": [],
            }
        violations = _cppcheck.to_violations(self.store, result.findings, self.config)
        by_severity: dict[str, int] = {}
        payload = []
        for v in violations:
            by_severity[v.severity.name] = by_severity.get(v.severity.name, 0) + 1
            payload.append({
                "code": v.code,
                "severity": v.severity.name,
                "file": v.file,
                "line": v.line,
                "symbol_id": v.symbol_id,
                "message": v.message,
                "confidence": v.confidence,
                "detail": v.detail,
            })
        return {
            "available": True,
            "tool_version": result.tool_version,
            "files_analyzed": result.files_analyzed,
            "finding_count": len(payload),
            "by_severity": by_severity,
            "findings": payload,
        }

    def execute_transaction(
        self,
        operations: list[dict[str, Any]],
        validate: list[str] | None = None,
        commit: bool = True,
        base_hashes: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Apply a list of edits atomically: snapshot -> apply -> reindex -> validate -> commit/rollback."""
        return _transaction.execute_transaction(
            self, operations, validate=validate, commit=commit, base_hashes=base_hashes
        )

    def edit_and_validate(
        self, operation: dict[str, Any], validate: list[str] | None = None
    ) -> dict[str, Any]:
        """One edit, then reindex + validate + commit-or-rollback (ENHANCEME §Q)."""
        return _transaction.edit_and_validate(self, operation, validate)

    def ast_transform(
        self,
        pattern: str,
        replacement: str,
        scope: str | None = None,
        language: str | None = None,
        include_tests: bool = True,
        max_files: int | None = None,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Multi-file ast-grep rewrite across a scope, atomic + validated (ENHANCEME §E)."""
        return _transform.ast_transform(
            self,
            pattern,
            replacement,
            scope=scope,
            language=language,
            include_tests=include_tests,
            max_files=max_files,
            validate=validate,
            commit=commit,
        )

    def transform(
        self,
        select: dict[str, Any],
        steps: list[dict[str, Any]],
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """The intent-level transformation DSL: select a target, apply steps (ENHANCEME §R)."""
        return _transform.transform(self, select, steps, validate=validate, commit=commit)

    # -- autonomous local execution (ENHANCEME §3-§8, §16) ------------

    def plan(self, intent: str, scope: str | None = None) -> dict[str, Any]:
        """Resolve a natural-language intent to an operation + blast-radius estimate."""
        return _planner.plan(self, intent, scope)

    def execute_plan(self, plan_id: str, commit: bool = True) -> dict[str, Any]:
        """Run a previously `plan()`-ed operation; refuses if the workspace moved since."""
        return _planner.execute(self, plan_id, commit=commit)

    def task(
        self,
        intent: str,
        scope: str | None = None,
        validate: bool = True,
        commit: bool = True,
        repair: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """State an intent; the engine plans, executes, repairs, and returns only counts."""
        return _planner.task(
            self, intent, scope, validate=validate, commit=commit, repair=repair
        )

    def resume_task(self, task_id: str) -> dict[str, Any]:
        """Re-run a recorded task from its stored intent (re-derives plan+execute fresh)."""
        return _planner.resume_task(self, task_id)

    def task_status(self, task_id: str) -> dict[str, Any]:
        """One recorded task's status + stored summary."""
        return _planner.task_status(self, task_id)

    def list_tasks(self, limit: int = 20) -> dict[str, Any]:
        """Recent recorded tasks, newest first."""
        return _planner.list_tasks(self, limit)

    def failure_context(self, diagnostic_id: str, detail: str | None = None) -> dict[str, Any]:
        """The minimum context for one diagnose() entry; source only when `detail='source'`."""
        return _planner.failure_context(self, diagnostic_id, detail)

    def repository_summary(self) -> dict[str, Any]:
        """A deterministic, compressed representation of the whole repository. No source."""
        return _planner.repository_summary(self)

    def inspect(self, target: dict[str, Any], detail: str | None = None) -> dict[str, Any]:
        """One adaptive read — file structure / symbol / range / diagnostic; source only on detail='source'."""
        return _inspect(self, target, detail)

    def context_for_task(self, symbol: str, file: str | None = None) -> dict[str, Any]:
        """The minimum change-impact subgraph for one symbol — counts first, no source (ENHANCEME §17)."""
        from code_intelligence.core.context.task_context import context_for_task

        return context_for_task(self, symbol, file)

    def refactor_capabilities(self) -> dict[str, Any]:
        """`{language: [supported refactor ops]}` — the honest capability surface (ENHANCEME §19)."""
        from code_intelligence.core.context.refactor_backends import capability_matrix

        return {"languages": capability_matrix()}

    # -- semantic refactoring primitives (ENHANCEME §F, §W) ------------

    def add_import(
        self,
        file: str,
        module: str,
        name: str | None = None,
        alias: str | None = None,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Add an import to a Python file, placed and de-duplicated (LibCST)."""
        return _refactor.add_import(
            self, file, module, name, alias, validate=validate, commit=commit
        )

    def organize_imports(
        self,
        scope: str | None = None,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Sort imports + drop unused ones across a scope (ruff), as one transaction."""
        return _refactor.organize_imports(self, scope, validate=validate, commit=commit)

    def remove_unused_imports(
        self,
        scope: str | None = None,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Drop unused imports across a scope (ruff F401), as one transaction."""
        return _refactor.remove_unused_imports(self, scope, validate=validate, commit=commit)

    def extract_function(
        self,
        file: str,
        start_line: int,
        end_line: int,
        new_name: str,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Extract a run of statements into a new top-level function; refuses when it escapes scope."""
        return _refactor.extract_function(
            self, file, start_line, end_line, new_name, validate=validate, commit=commit
        )

    # -- code actions + recipes (ENHANCEME §S, §T) ---------------------

    def code_actions(self, file: str, line: int) -> dict[str, Any]:
        """Machine-discovered transformations that apply at `file`:`line`, each with a ready call."""
        return _code_actions.code_actions(self, file, line)

    def apply_code_action(
        self, file: str, line: int, action_id: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Fill an action's placeholders from `params` and dispatch it."""
        return _code_actions.apply_code_action(self, file, line, action_id, params)

    def list_recipes(self) -> dict[str, str]:
        """Every refactoring recipe name and its one-line description."""
        return _recipes.list_recipes()

    def run_recipe(
        self, name: str, args: dict[str, Any] | None = None, commit: bool = True
    ) -> dict[str, Any]:
        """Run a named refactoring recipe over the primitives (ENHANCEME §T)."""
        return _recipes.run_recipe(self, name, args, commit)

    def change_signature(
        self,
        file: str,
        symbol: str,
        add_parameter: dict[str, Any] | None = None,
        parameters: list[dict[str, Any]] | None = None,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Change a Python function's signature.

        `parameters` (the full new ordered list of `{name, default?}`)
        reorders/adds/removes and rewrites call sites to keyword form;
        `add_parameter` is the additive shortcut that touches no call site.
        """
        if parameters is not None:
            return _py_refactor2.change_signature_full(
                self, file, symbol, parameters, validate=validate, commit=commit
            )
        return _refactor.change_signature(
            self, file, symbol, add_parameter, validate=validate, commit=commit
        )

    def rename_parameter(
        self,
        file: str,
        symbol: str,
        old_name: str,
        new_name: str,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Rename a Python parameter in the def, its body, and `name=` call-site keywords."""
        return _py_refactor2.rename_parameter(
            self, file, symbol, old_name, new_name, validate=validate, commit=commit
        )

    def move_symbol(
        self,
        symbol: str,
        from_file: str,
        to_file: str,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Move a top-level Python function/class between files, repairing imports."""
        return _py_refactor2.move_symbol(
            self, symbol, from_file, to_file, validate=validate, commit=commit
        )

    def inline_function(
        self,
        file: str,
        symbol: str,
        validate: list[str] | None = None,
        commit: bool = True,
    ) -> dict[str, Any]:
        """Inline a trivial `return <expr>` Python function into its call sites; refuses otherwise."""
        return _refactor.inline_function(self, file, symbol, validate=validate, commit=commit)

    # -- C++ include/macro/type layer (ENHANCEME §V) ------------------

    def find_includes(self, file: str) -> dict[str, Any]:
        """The `#include` directives in one C++ file."""
        return _cpp_includes.find_includes(self, file)

    def include_graph(self, path_prefix: str | None = None, limit: int = 40) -> dict[str, Any]:
        """The workspace `#include` DAG and the most-included headers."""
        return _cpp_includes.include_graph(self, path_prefix, limit)

    def add_include(
        self, file: str, include: str, system: bool = False,
        validate: list[str] | None = None, commit: bool = True,
    ) -> dict[str, Any]:
        """Add a `#include` to a C++ file, ordered and de-duplicated."""
        return _cpp_includes.add_include(self, file, include, system, validate=validate, commit=commit)

    def remove_include(
        self, file: str, include: str,
        validate: list[str] | None = None, commit: bool = True,
    ) -> dict[str, Any]:
        """Remove a `#include` line from a C++ file."""
        return _cpp_includes.remove_include(self, file, include, validate=validate, commit=commit)

    def find_macro(
        self, name: str, path_prefix: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        """The `#define` and textual uses of a C++ macro across the workspace."""
        return _cpp_macros.find_macro(self, name, path_prefix, limit)

    def find_overrides(self, method: str, translation_unit: str) -> dict[str, Any]:
        """Methods overriding / overridden by `method`, exact via libclang (one TU)."""
        return _cpp_queries.find_overrides(self.root, method, translation_unit)

    def find_implementations(self, interface: str, translation_unit: str) -> dict[str, Any]:
        """Classes deriving from `interface` in one TU, exact via libclang."""
        return _cpp_queries.find_implementations(self.root, interface, translation_unit)

    def find_virtual_callers(self, method: str, translation_unit: str) -> dict[str, Any]:
        """Virtual-dispatch call sites of `method` in one TU, exact via libclang."""
        return _cpp_queries.find_virtual_callers(self.root, method, translation_unit)

    def type_uses(self, type_name: str, translation_unit: str, limit: int = 200) -> dict[str, Any]:
        """Declarations mentioning `type_name` in one TU, exact via libclang."""
        return _cpp_queries.type_uses(self.root, type_name, translation_unit, limit)

    def constructor_calls(self, class_name: str, translation_unit: str) -> dict[str, Any]:
        """Where `class_name` is constructed in one TU, exact via libclang."""
        return _cpp_queries.constructor_calls(self.root, class_name, translation_unit)

    def macro_expansion_context(
        self, file: str, line: int, translation_unit: str
    ) -> dict[str, Any]:
        """The macro expanded at `file`:`line` and its `#define`, via libclang."""
        return _cpp_queries.macro_expansion_context(self.root, file, line, translation_unit)

    def forward_declare(
        self, file: str, symbol: str, kind: str = "class", namespace: str | None = None,
        remove_include: str | None = None,
        validate: list[str] | None = None, commit: bool = True,
    ) -> dict[str, Any]:
        """Add a forward declaration to a C++ file (optionally dropping an include)."""
        return _cpp_transform.forward_declare(
            self, file, symbol, kind, namespace, remove_include, validate=validate, commit=commit
        )

    def change_return_type(
        self, file: str, symbol: str, new_type: str,
        rewrite_call_sites: bool = False,
        validate: list[str] | None = None, commit: bool = True,
    ) -> dict[str, Any]:
        """Change a C++ function's return type.

        `rewrite_call_sites=True` (needs `compile_commands.json`) also folds
        the mechanically-safe call-site edits into the transaction via
        libclang and returns the rest in `review`; otherwise call sites are
        only reported.
        """
        return _cpp_transform.change_return_type(
            self, file, symbol, new_type, rewrite_call_sites=rewrite_call_sites,
            validate=validate, commit=commit,
        )

    def change_parameter_type(
        self, file: str, symbol: str, parameter: str, new_type: str,
        validate: list[str] | None = None, commit: bool = True,
    ) -> dict[str, Any]:
        """Change one C++ parameter's type; reports call sites."""
        return _cpp_transform.change_parameter_type(
            self, file, symbol, parameter, new_type, validate=validate, commit=commit
        )

    def change_type(
        self, file: str, line: int, old_type: str, new_type: str,
        validate: list[str] | None = None, commit: bool = True,
    ) -> dict[str, Any]:
        """Replace a type name on one C++ line (a local declaration)."""
        return _cpp_transform.change_type(
            self, file, line, old_type, new_type, validate=validate, commit=commit
        )

    def change_summary(self, ref: str = "working-tree") -> dict[str, Any]:
        """A compact, counts-first semantic diff against `ref` (ENHANCEME §K).

        Wraps `diff(ref)` -- same underlying re-parse of just the changed
        files -- into a payload an agent reads at a glance: how many files
        and symbols moved, whether any public API symbol changed, how the
        diagnostic count shifted, and which test files the changed symbols
        reach. The full lists stay available via `diff`.
        """
        detail = self.diff(ref)
        changed_symbols = detail["changed_symbols"]
        added, removed = detail["added_symbols"], detail["removed_symbols"]

        test_files: set[str] = set()
        for symbol_id in changed_symbols + added:
            try:
                report = impact_module.affected_tests(self.store, symbol_id)
            except LookupError:
                continue
            test_files.update(report["test_files"])

        def _is_public(symbol_id: str) -> bool:
            row = self.store.get_symbol(symbol_id)
            return bool(row) and not row["name"].startswith("_")

        api_changed = sorted(s for s in changed_symbols + removed if _is_public(s))

        return {
            "ref": ref,
            "counts": {
                "changed_files": len(detail["changed_files"]),
                "symbols_changed": len(changed_symbols),
                "symbols_added": len(added),
                "symbols_removed": len(removed),
                "public_api_changed": len(api_changed),
                "new_violations": len(detail["new_violations"]),
                "resolved_violations": len(detail["resolved_violations"]),
                "test_files_affected": len(test_files),
            },
            "changed_files": detail["changed_files"],
            "public_api_changed": api_changed,
            "test_files_affected": sorted(test_files),
        }

    # -- violations / range -----------------------------------------------

    def refresh_semantic(
        self,
        changed_files: set[str] | None = None,
        languages: list[str] | None = None,
        limit: int | None = None,
        batch_size: int | None = None,
    ) -> dict[str, Any]:
        """Re-extract exact references for whatever went stale.

        `batch_size` commits as it goes. It matters most exactly where the
        cost is worst: a C++ pass killed halfway through 300 translation
        units keeps every batch it finished, instead of losing the hour.
        """
        report = semantic.refresh(
            self.store,
            self.root,
            changed_files=changed_files,
            languages=languages,
            limit=limit,
            batch_size=batch_size,
        )
        self.store.bump_revision("semantic")
        self.store.connection.commit()
        return report

    def semantic_coverage(self) -> dict[str, Any]:
        """How much of the index is backed by exact data, per language."""
        report = semantic_store.coverage(self.store)
        report["pending"] = self._pending_units()
        return report

    def unreferenced_symbols(
        self,
        path_prefix: str | None = None,
        language: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Candidates for dead code, from exact edges -- or a refusal.

        Raises when the semantic index is incomplete for a language in
        scope, because an uncovered caller and an absent caller produce the
        same empty answer and only one of them means "safe to delete".
        """
        return deadcode.unreferenced_symbols(
            self.store, self._pending_units(), path_prefix, language, limit
        )

    def untested_symbols(
        self,
        path_prefix: str | None = None,
        language: str | None = None,
        min_loc: int | None = None,
        include_private: bool | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        """Functions and methods no test file reaches -- or a refusal.

        Raises for the same reason `unreferenced_symbols` does: a test that
        lives in an unextracted translation unit and a test that does not
        exist produce the same empty answer, and acting on the wrong one
        means writing a test that already exists.

        `min_loc` and `include_private` default to the workspace config's
        `untested_symbols` block, so a caller that passes neither gets the
        thresholds the project chose rather than this module's.
        """
        settings = self.config.get("untested_symbols", {})
        return testcoverage.untested_symbols(
            self.store,
            self._pending_units(),
            path_prefix,
            language,
            settings.get("min_loc", 5) if min_loc is None else min_loc,
            settings.get("include_private", False)
            if include_private is None
            else include_private,
            limit,
        )

    def _pending_units(self) -> dict[str, Any]:
        """Per language: units a backend claims, and how many are not done yet.

        Delegates to `core.semantic.coverage`, which the indexer also needs
        and which cannot import this module without a cycle.
        """
        return semantic_coverage_module.pending_units(self.store, self.root)

    def exact_references(self, file: str, line: int) -> dict[str, Any]:
        """Call sites resolving to the definition at `file`:`line`, exactly.

        Returns `{"available": False, ...}` rather than an empty list when
        no backend has covered that file: "not analyzed" and "not called"
        must never look alike.
        """
        row = self.store.connection.execute(
            "SELECT ok, error, backend, language FROM semantic_units WHERE unit = ?", (file,)
        ).fetchone()
        edges = semantic_store.references_to(self.store, file, line)
        covered_by = self.store.connection.execute(
            "SELECT count(*) c FROM semantic_units WHERE ok = 1"
        ).fetchone()["c"]
        return {
            "file": file,
            "line": line,
            "confidence": semantic.CONFIDENCE_EXACT,
            "available": covered_by > 0,
            "definition_unit_analyzed": bool(row and row["ok"]),
            "definition_unit_error": row["error"] if row else None,
            "references": edges,
            "count": len(edges),
        }

    def semantic_backends(self) -> dict[str, Any]:
        """Which languages have an exact (type-aware) backend available here."""
        return semantic.semantic_backends(self.root)

    def clang_references(
        self, method_name: str, class_name: str | None, translation_unit: str
    ) -> dict[str, Any]:
        """Exact C++ call sites of one method within one translation unit."""
        return semantic.clang_references(self.root, method_name, class_name, translation_unit)

    def outline_symbol(self, file: str, symbol: str, max_depth: int = 2) -> dict[str, Any] | None:
        """The control-flow shape of one symbol -- see core.context.inspection."""
        return inspection.outline_symbol(self.store, self.root, file, symbol, max_depth)

    def search_text(
        self,
        pattern: str,
        path_prefix: str | None = None,
        language: str | None = None,
        include_tests: bool = True,
        is_regex: bool = True,
        case_sensitive: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Search indexed files, each hit tagged with its enclosing symbol."""
        return searching.search_text(
            self.store, self.root, pattern, path_prefix, language,
            include_tests, is_regex, case_sensitive, limit,
        )

    def symbol_source(self, file: str, symbol: str) -> dict[str, Any]:
        """One symbol's source text plus the hash needed to replace it."""
        return editing.symbol_source(self.store, self.root, file, symbol)

    def replace_symbol(
        self, file: str, symbol: str, new_text: str, expected_hash: str
    ) -> dict[str, Any]:
        """Replace a symbol's definition, gated on it being unchanged."""
        return editing.replace_symbol(self.store, self.root, file, symbol, new_text, expected_hash)

    def insert_lines(
        self, file: str, line: int, text: str, expected_hash: str, anchor_lines: int = 3
    ) -> dict[str, Any]:
        """Insert text before `line`, gated on the preceding lines being unchanged."""
        return editing.insert_lines(
            self.store, self.root, file, line, text, expected_hash, anchor_lines
        )

    def anchor_hash(self, file: str, line: int, anchor_lines: int = 3) -> dict[str, Any]:
        """The hash `insert_lines` will expect at `line`."""
        return editing.anchor_hash(self.root, file, line, anchor_lines)

    def rename_symbol(
        self, file: str, symbol: str, new_name: str, dry_run: bool = True
    ) -> dict[str, Any]:
        """Rename a symbol's definition and every exact reference.

        C++ routes through clangd (type-accurate, cross-file via its
        background index) when a compilation database and `clangd` are
        available; other languages use the exact-backend path in
        `core.context.editing`, which refuses without exact coverage.
        """
        file_row = self.store.get_file(file)
        if file_row is not None and file_row["language"] == "cpp":
            from code_intelligence.core.context import cpp_rename

            try:
                return cpp_rename.rename_symbol(self, file, symbol, new_name, dry_run=dry_run)
            except cpp_rename.ClangdUnavailable:
                pass  # fall through to the heuristic path (which will refuse)
        return editing.rename_symbol(self.store, self.root, file, symbol, new_name, dry_run)

    def ast_search(
        self,
        pattern: str,
        language: str | None = None,
        path_prefix: str | None = None,
        include_tests: bool = True,
        limit: int = 50,
    ) -> dict[str, Any]:
        """Every indexed-file match of an ast-grep structural pattern (`$VAR`/`$$$VAR`)."""
        return structural.ast_search(
            self.store, self.root, pattern, language, path_prefix, include_tests, limit
        )

    def ast_replace(
        self,
        file: str,
        pattern: str,
        replacement: str,
        expected_hash: str,
        language: str | None = None,
        dry_run: bool = True,
    ) -> dict[str, Any]:
        """Rewrite every match of an ast-grep pattern in one file, gated on it being unchanged."""
        return structural.ast_replace(
            self.store, self.root, file, pattern, replacement, expected_hash, language, dry_run
        )

    def rank_symbols(
        self,
        metric: str = "cyclomatic_complexity",
        path_prefix: str | None = None,
        kind: str | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """The worst symbols by `metric` -- see core.context.inspection."""
        return inspection.rank_symbols(self.store, metric, path_prefix, kind, limit)

    def summarize_violations(
        self, path_prefix: str | None = None, min_severity: str | None = None
    ) -> dict[str, Any]:
        """Counts of findings by severity/rule/area -- see core.context.inspection."""
        return inspection.summarize_violations(self.store, path_prefix, min_severity)

    def file_report(self, path: str) -> dict[str, Any] | None:
        """One file's size, symbols and findings -- see core.context.inspection."""
        return _cached(
            self,
            "file_report",
            {"path": path},
            lambda: inspection.file_report(self.store, path),
        )

    def block_range(self, path: str, line: int) -> dict[str, Any] | None:
        """Exact line range of the block containing `line` -- see core.context.inspection."""
        return inspection.block_range(self.root, path, line)

    def get_violations(self, severity: str | None = None) -> list[dict[str, Any]]:
        """Every diagnostic at/above `severity` (default: every diagnostic)."""
        return context_module.get_violations(self.store, severity)

    def check_markdown_links(
        self, subdir: str = ".", index_file: str | None = "Home.md"
    ) -> dict[str, Any]:
        """Orphaned pages and broken relative links across a directory of `.md` files.

        Filesystem-only (does not touch the symbol index) -- see
        `core.filesystem.markdown_links`. Meant for a wiki directory
        (`subdir="software/nn/.wiki"`), but works over any `.md` tree.

        Raises:
            PathTraversalError: `subdir` resolves outside this workspace's root (fixme §37).
        """
        base = resolve_within_workspace(self.root, subdir)
        return check_markdown_links(base, index_file)

    def detect_toolchain(self) -> dict[str, Any]:
        """Which build/test/lint/format toolchain(s) this project declares, and what's installed."""
        return exec_module.detect_toolchain(self.root)

    def run_build(self, target: str | None = None, timeout: int | None = None) -> dict[str, Any]:
        """Build the project -- see core.exec.runner."""
        kwargs = {} if timeout is None else {"timeout": timeout}
        return exec_module.run_build(self.root, target, **kwargs)

    def run_tests(
        self,
        toolchain: str | None = None,
        filter: str | None = None,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        """Run the project's test suite -- see core.exec.runner."""
        kwargs = {} if timeout is None else {"timeout": timeout}
        return exec_module.run_tests(self.root, toolchain, filter, **kwargs)

    def run_lint(self, timeout: int | None = None) -> dict[str, Any]:
        """Run the project's linter -- see core.exec.runner."""
        kwargs = {} if timeout is None else {"timeout": timeout}
        return exec_module.run_lint(self.root, **kwargs)

    def run_format(self, check_only: bool = True, timeout: int | None = None) -> dict[str, Any]:
        """Format (or check formatting of) the project -- see core.exec.runner."""
        kwargs = {} if timeout is None else {"timeout": timeout}
        return exec_module.run_format(self.root, check_only, **kwargs)

    def git_status(self) -> dict[str, Any]:
        """Current branch/upstream/ahead-behind plus staged/unstaged/untracked files."""
        return git_status_module.git_status(self.root)

    def git_log(self, path: str | None = None, ref: str = "HEAD", limit: int = 20) -> list[dict[str, Any]]:
        """Commit history for `ref`, optionally scoped to one `path`."""
        return git_log_module.git_log(self.root, path, ref, limit)

    def git_blame(
        self, file: str, start_line: int | None = None, end_line: int | None = None
    ) -> list[dict[str, Any]]:
        """Per-line blame for `file`, optionally restricted to `[start_line, end_line]`."""
        return git_blame_module.git_blame(self.root, file, start_line, end_line)

    def git_diff_stat(self, ref: str = WORKING_TREE) -> dict[str, Any]:
        """Per-file insertions/deletions relative to `ref` (`working-tree`, `staged`, or a git ref)."""
        return diff_stat_for(self.root, ref)

    def read_range(self, file: str, start_line: int, end_line: int) -> dict[str, Any]:
        """Read an exact line range from disk; reports a content_hash and any overlapping symbol_id.

        Raises:
            PathTraversalError: `file` resolves outside this workspace's root (fixme §37).
        """
        path = resolve_within_workspace(self.root, file)
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        clamped_end = min(end_line, len(lines)) if lines else end_line
        content = "\n".join(lines[start_line - 1 : clamped_end])

        matched_symbol_id = None
        for row in self.store.list_symbols_for_file(file):
            if row["start_line"] <= start_line and row["end_line"] >= clamped_end:
                matched_symbol_id = row["symbol_id"]
                break

        file_row = self.store.get_file(file)
        return {
            "file": file,
            "start_line": start_line,
            "end_line": clamped_end,
            "language": file_row["language"] if file_row else None,
            "symbol_id": matched_symbol_id,
            "content": content,
            "content_hash": content_hash(content),
        }

    # -- ref-based diff (fixme §35) ----------------------------------------

    def diff(self, ref: str) -> dict[str, Any]:
        """Compare the workspace against a comparison point (fixme §35).

        `ref` is `"working-tree"` (current on-disk content vs `HEAD` —
        mirrors plain `git diff`), `"staged"` (the git index vs `HEAD` —
        mirrors `git diff --cached`), or any other git ref (`"HEAD"`,
        `"HEAD~1"`, a commit sha, a branch name — compared against the
        current on-disk working tree). Returns `{changed_files,
        added_symbols, removed_symbols, changed_symbols, new_violations,
        resolved_violations, regressions}`. Re-parses only the changed
        files at both points (via `git show`/on-disk read) — never a
        whole-repo reparse.
        """
        changed_relpaths = [
            relpath for relpath in changed_files_for(self.root, ref) if self._is_analyzable(relpath)
        ]

        # `ref` names the *old* side conceptually, but "working-tree"/"staged"
        # are each themselves compared against HEAD (mirroring plain `git
        # diff` / `git diff --cached`) — only a real ref (HEAD, HEAD~N, a
        # commit, a branch) is used directly as the old side, always
        # compared against the current on-disk working tree as the new side.
        if ref == WORKING_TREE:
            old_point, new_point = "HEAD", WORKING_TREE
        elif ref == STAGED:
            old_point, new_point = "HEAD", STAGED
        else:
            old_point, new_point = ref, WORKING_TREE

        old_symbol_records: list = []
        new_symbol_records: list = []
        old_violation_dicts: list[dict[str, Any]] = []
        new_violation_dicts: list[dict[str, Any]] = []

        for relpath in changed_relpaths:
            old_text = content_at(self.root, old_point, relpath)
            new_text = content_at(self.root, new_point, relpath)

            if old_text is not None:
                old_records, old_viols = self._analyze_snapshot(relpath, old_text)
                old_symbol_records.extend(old_records)
                old_violation_dicts.extend(old_viols)
            if new_text is not None:
                new_records, new_viols = self._analyze_snapshot(relpath, new_text)
                new_symbol_records.extend(new_records)
                new_violation_dicts.extend(new_viols)

        old_by_id = {record.symbol_id: record for record in old_symbol_records}
        new_by_id = {record.symbol_id: record for record in new_symbol_records}
        changed_ids = {
            symbol_id
            for symbol_id, new_record in new_by_id.items()
            if symbol_id in old_by_id and old_by_id[symbol_id].content_hash != new_record.content_hash
        }

        result = {"changed_files": changed_relpaths}
        result.update(symbol_diff(set(old_by_id), set(new_by_id), changed_ids))
        result.update(violation_diff(old_violation_dicts, new_violation_dicts))
        return result

    def _is_analyzable(self, relpath: str) -> bool:
        """Return True when `relpath`'s extension is one this workspace's config analyzes."""
        extensions: set[str] = set()
        for lang_exts in self.config["languages"].values():
            extensions.update(lang_exts)
        return Path(relpath).suffix in extensions

    def _analyze_snapshot(self, relpath: str, source: str) -> tuple[list, list[dict[str, Any]]]:
        """Parse+rule-check one ad-hoc snapshot of `relpath`'s content (not persisted)."""
        adapter = find_adapter(Path(relpath))
        if adapter is None:
            return [], []
        analysis = adapter.analyze(self.root / relpath, source)
        analysis.relpath = relpath
        generated.annotate(analysis, source, self.config)
        assign_symbol_ids(analysis)
        violations = run_file_rules(analysis, self.config)
        for violation in violations:
            violation.symbol_id = find_symbol_id_for_line(analysis, violation.line)
        symbols = flatten_symbols(analysis, source)
        violation_dicts = [
            {
                "code": violation.code,
                "severity": violation.severity.name,
                "file": violation.file,
                "line": violation.line,
                "end_line": violation.end_line,
                "symbol_id": violation.symbol_id,
                "message": violation.message,
            }
            for violation in violations
        ]
        return symbols, violation_dicts


__all__ = ["Workspace"]
