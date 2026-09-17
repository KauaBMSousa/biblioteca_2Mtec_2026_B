"""A local transaction engine for multi-step edits (ENHANCEME §C, §D, §P, §Q).

The agent should never have to coordinate rollback by hand, remember to
reindex after an edit, or thread an `expected_hash` through a chain of
edits that each invalidate the last one's hash. `execute_transaction`
absorbs all of that:

    snapshot every file the operations will touch
        -> apply the operations in order (hashes recomputed per step)
        -> incrementally reindex (so validation sees fresh state)
        -> validate: syntax / lint / affected tests
        -> commit, or restore every snapshotted file and reindex

An operation that raises, a `rename_symbol` that comes back `blocked`, a
validation step that fails, a new diagnostic regression, or `commit=False`
all lead to the same place: the filesystem is put back exactly as it was
and the index is brought back in step. Nothing is left half-applied.

The staleness guard the individual editing primitives enforce still
applies at the transaction boundary: pass `base_hashes` ({relpath:
content_hash}) and the whole transaction refuses before touching anything
if any of those files changed underneath. Inside the transaction the
per-step hashes are recomputed from current disk state, so chaining edits
that build on each other just works.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from code_intelligence.core.context.editing import (
    LowConfidenceRenameError,
    StaleEditError,
    write_file,
)
from code_intelligence.core.context.impact import affected_tests as _affected_tests
from code_intelligence.core.diagnostics.violation import Severity
from code_intelligence.core.hashes.content_hash import content_hash
from code_intelligence.core.parser import find_adapter

#: Diagnostics at or above this severity are the ones a transaction treats
#: as a regression when their count for a touched file goes up.
_REGRESSION_FLOOR = Severity["HIGH"].value

_EDIT_OPS = {"replace_symbol", "insert_lines", "rename_symbol", "ast_replace", "write_file"}
_RECOVERABLE = (StaleEditError, LowConfidenceRenameError, LookupError, ValueError)


@dataclass(slots=True)
class _Op:
    """One normalized operation from the caller's plan."""

    kind: str
    params: dict[str, Any]


def _read(path: Path) -> str | None:
    return path.read_text(encoding="utf-8", errors="replace") if path.exists() else None


def _target_files(workspace: Any, op: _Op) -> set[str]:
    """Every file `op` may write — including a rename's call-site files."""
    files = {op.params["file"]}
    if op.kind == "rename_symbol":
        plan = workspace.rename_symbol(
            op.params["file"], op.params["symbol"], op.params["new_name"], dry_run=True
        )
        files.update(site["file"] for site in plan.get("call_sites", []))
    return files


def _apply_op(workspace: Any, op: _Op) -> dict[str, Any]:
    """Run one edit against current disk state, recomputing its staleness hash first."""
    p = op.params
    if op.kind == "replace_symbol":
        current = workspace.symbol_source(p["file"], p["symbol"])
        return workspace.replace_symbol(
            p["file"], p["symbol"], p["new_text"], current["content_hash"]
        )
    if op.kind == "insert_lines":
        anchor_lines = p.get("anchor_lines", 3)
        current = workspace.anchor_hash(p["file"], p["line"], anchor_lines)
        return workspace.insert_lines(
            p["file"], p["line"], p["text"], current["content_hash"], anchor_lines
        )
    if op.kind == "rename_symbol":
        return workspace.rename_symbol(p["file"], p["symbol"], p["new_name"], dry_run=False)
    if op.kind == "ast_replace":
        current_hash = content_hash(_read(workspace.root / p["file"]) or "")
        return workspace.ast_replace(
            p["file"], p["pattern"], p["replacement"], current_hash, p.get("language"), dry_run=False
        )
    if op.kind == "write_file":
        current_hash = content_hash(_read(workspace.root / p["file"]) or "")
        return write_file(workspace.store, workspace.root, p["file"], p["new_text"], current_hash)
    raise ValueError(f"unknown transaction op {op.kind!r}")


def _op_summary(kind: str, result: dict[str, Any]) -> dict[str, Any]:
    """The handful of fields worth keeping from an operation's full result."""
    if kind == "rename_symbol":
        return {
            "blocked": result.get("blocked", False),
            "blocking_reasons": result.get("blocking_reasons"),
            "call_sites": result.get("call_site_count"),
        }
    if kind == "ast_replace":
        return {"match_count": result.get("match_count")}
    if kind == "replace_symbol":
        return {"line_delta": result.get("line_delta")}
    return {}


def _restore(root: Path, snapshot: dict[str, str | None]) -> None:
    """Put every snapshotted file back to its original bytes (or remove one that did not exist)."""
    for relpath, original in snapshot.items():
        path = root / relpath
        if original is None:
            path.unlink(missing_ok=True)
        else:
            path.write_text(original, encoding="utf-8")


def _diag_count(store: Any, files: set[str]) -> int:
    """Count of HIGH+ diagnostics across `files`."""
    return sum(
        1
        for relpath in files
        for row in store.list_diagnostics(file_path=relpath)
        if row["severity"] >= _REGRESSION_FLOOR
    )


def _validate_syntax(workspace: Any, files: set[str]) -> dict[str, Any]:
    """Re-parse each touched file with its adapter; fail on any parse error."""
    broken: list[dict[str, str]] = []
    for relpath in sorted(files):
        path = workspace.root / relpath
        if not path.exists():
            continue
        adapter = find_adapter(path)
        if adapter is None:
            continue
        analysis = adapter.analyze(path, path.read_text(encoding="utf-8", errors="replace"))
        if not analysis.parse_ok:
            broken.append({"file": relpath, "error": analysis.parse_error or "parse failed"})
    return {"status": "passed" if not broken else "failed", "parse_failures": broken}


def _validate_lint(workspace: Any) -> dict[str, Any]:
    """Run the project linter; `ISSUES_FOUND` is a failure, a missing linter is a skip."""
    try:
        result = workspace.run_lint()
    except Exception as exc:  # noqa: BLE001 - a missing linter must not fail the transaction
        return {"status": "skipped", "reason": str(exc)}
    return {
        "status": "passed" if result["status"] == "PASSED" else "failed",
        "issue_count": result.get("issue_count"),
        "tool_status": result["status"],
    }


def _validate_tests(workspace: Any, touched_symbol_ids: set[str]) -> dict[str, Any]:
    """Run only the tests the touched symbols reach; a change no test reaches is a skip, not a pass."""
    stems: set[str] = set()
    test_files: set[str] = set()
    for symbol_id in touched_symbol_ids:
        try:
            report = _affected_tests(workspace.store, symbol_id)
        except LookupError:
            continue
        test_files.update(report["test_files"])
        if report["filter"]:
            stems.update(report["filter"].split("|"))
    if not stems:
        return {"status": "skipped", "reason": "no affected test file identified"}
    run = workspace.diagnose(scope="tests", filter="|".join(sorted(stems)))
    tests = run["tests"]
    if tests["status"] == "SKIPPED":
        return {"status": "skipped", "reason": tests.get("reason")}
    return {
        "status": "passed" if tests["status"] == "PASSED" else "failed",
        "selected_test_files": sorted(test_files),
        "counts": tests.get("counts"),
        "failures": tests.get("failures"),
    }


def _touched_symbol_ids(store: Any, files: set[str]) -> set[str]:
    """Every symbol currently indexed in `files` (post-reindex)."""
    return {
        row["symbol_id"] for relpath in files for row in store.list_symbols_for_file(relpath)
    }


def execute_transaction(
    workspace: Any,
    operations: list[dict[str, Any]],
    *,
    validate: list[str] | None = None,
    commit: bool = True,
    base_hashes: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Apply `operations` atomically, validate, then commit or roll everything back.

    `operations` is a list of `{op, ...}` dicts; `op` is one of
    `replace_symbol`, `insert_lines`, `rename_symbol`, `ast_replace` (same
    params as the standalone tools, but no `expected_hash` — the
    transaction recomputes it per step).

    `validate` is any subset of `["syntax", "lint", "tests"]`. `"tests"`
    runs only the tests the touched symbols reach.

    `commit=False` applies and validates exactly as a real run would, then
    always rolls back — a true dry run that tells you whether the whole
    plan would have succeeded.

    Returns a compact result: `outcome` is one of `committed`,
    `validated_dry_run`, `validation_failed`, `apply_failed`,
    `stale_base`. No source is returned.
    """
    validate = validate or []
    root: Path = workspace.root
    store = workspace.store

    ops = [_Op(entry["op"], entry) for entry in operations]
    bad = [op.kind for op in ops if op.kind not in _EDIT_OPS]
    if bad:
        raise ValueError(f"unsupported transaction op(s): {sorted(set(bad))}")

    if base_hashes:
        for relpath, expected in base_hashes.items():
            actual = content_hash(_read(root / relpath) or "")
            if actual != expected:
                return {
                    "outcome": "stale_base",
                    "committed": False,
                    "rolled_back": False,
                    "file": relpath,
                    "expected": expected,
                    "actual": actual,
                }

    targets: set[str] = set()
    for op in ops:
        targets |= _target_files(workspace, op)
    snapshot = {relpath: _read(root / relpath) for relpath in targets}
    diags_before = _diag_count(store, targets)

    applied: list[dict[str, Any]] = []
    try:
        for op in ops:
            result = _apply_op(workspace, op)
            summary = _op_summary(op.kind, result)
            applied.append({"op": op.kind, "file": op.params.get("file"), **summary})
            if summary.get("blocked"):
                raise StaleEditError(
                    f"rename of {op.params.get('symbol')} is blocked: "
                    f"{summary.get('blocking_reasons')}"
                )
    except _RECOVERABLE as exc:
        _restore(root, snapshot)
        workspace.index()
        return {
            "outcome": "apply_failed",
            "committed": False,
            "rolled_back": True,
            "failed_at_operation": len(applied),
            "error": {"type": type(exc).__name__, "message": str(exc)},
            "operations": applied,
        }

    needs_semantic = any(op.kind == "rename_symbol" for op in ops)
    return finalize_change(
        workspace,
        snapshot,
        targets,
        diags_before,
        validate,
        commit,
        extra={"operations": applied},
        needs_semantic=needs_semantic,
    )


def snapshot_files(root: Path, files: set[str]) -> dict[str, str | None]:
    """Read every file in `files` (None for one that does not exist yet)."""
    return {relpath: _read(root / relpath) for relpath in files}


def finalize_change(
    workspace: Any,
    snapshot: dict[str, str | None],
    targets: set[str],
    diags_before: int,
    validate: list[str],
    commit: bool,
    *,
    extra: dict[str, Any] | None = None,
    needs_semantic: bool = False,
) -> dict[str, Any]:
    """The shared tail: reindex (§P) -> validate -> commit or restore + reindex.

    Used both by `execute_transaction` and by the refactoring primitives
    that mutate the filesystem through an external tool (`organize_imports`)
    rather than a list of ops.
    """
    root: Path = workspace.root
    store = workspace.store

    workspace.index(semantic_refresh=needs_semantic)
    touched_symbol_ids = _touched_symbol_ids(store, targets)

    validation: dict[str, Any] = {}
    if "syntax" in validate:
        validation["syntax"] = _validate_syntax(workspace, targets)
    if "lint" in validate:
        validation["lint"] = _validate_lint(workspace)
    if "tests" in validate:
        validation["tests"] = _validate_tests(workspace, touched_symbol_ids)

    diags_after = _diag_count(store, targets)
    failed_steps = [name for name, step in validation.items() if step["status"] == "failed"]
    regressed = diags_after > diags_before
    changed_files = sorted(
        relpath for relpath in targets if _read(root / relpath) != snapshot[relpath]
    )

    common = {
        "changed_files": changed_files,
        "changed_file_count": len(changed_files),
        "changed_symbols": len(touched_symbol_ids),
        "validation": validation,
        "diagnostics": {"before": diags_before, "after": diags_after, "regressed": regressed},
        **(extra or {}),
    }

    if failed_steps or regressed:
        _restore(root, snapshot)
        workspace.index()
        return {
            "outcome": "validation_failed",
            "committed": False,
            "rolled_back": True,
            "failed_validation": failed_steps or (["diagnostics"] if regressed else []),
            **common,
        }
    if not commit:
        _restore(root, snapshot)
        workspace.index()
        return {"outcome": "validated_dry_run", "committed": False, "rolled_back": True, **common}
    return {"outcome": "committed", "committed": True, "rolled_back": False, **common}


def edit_and_validate(
    workspace: Any, operation: dict[str, Any], validate: list[str] | None = None
) -> dict[str, Any]:
    """One edit, then reindex + validate + commit-or-rollback (ENHANCEME §Q).

    A single-operation `execute_transaction`. `validate` defaults to
    `["syntax", "tests"]`.
    """
    return execute_transaction(
        workspace,
        [operation],
        validate=validate if validate is not None else ["syntax", "tests"],
        commit=True,
    )


__all__ = ["execute_transaction", "edit_and_validate", "snapshot_files", "finalize_change"]
