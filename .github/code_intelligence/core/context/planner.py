"""Autonomous local execution: plan / execute / task / failure_context (ENHANCEME §3-§8).

`task(intent)` is the top of the agent-facing stack: one call states what
to do, the local engine resolves it to an operation, estimates its blast
radius, applies it through the transaction engine, runs a bounded
deterministic repair loop, and returns *counts* — no source, no diff, no
intermediate results. `plan()` / `execute(plan_id)` split the same flow
when the agent wants to look before it leaps.

`failure_context(diagnostic_id)` closes the loop the other way: `diagnose`
hands back stable ids, and this returns the minimum context for one — the
enclosing symbol and range, whether the current change touched it — with
source only on `detail="source"`.

Every `task()` run is recorded as `T<n>` (ENHANCEME §10): `task_status`,
`resume_task` (re-derives plan+execute fresh from the stored intent) and
`list_tasks` read that back. The escalation ladder (§6) is automatic — the
agent states intent, the engine picks and, on refusal, climbs the level.
"""

import hashlib
import json
from typing import Any

from code_intelligence.core.context.intent import parse_intent

_MAX_REPAIR_ITERATIONS = 3


# -- plan -----------------------------------------------------------------


def _plan_id(intent: str, revision: int) -> str:
    return "p" + hashlib.sha256(f"{intent}|{revision}".encode()).hexdigest()[:8]


def _resolve(workspace: Any, op: str, params: dict[str, Any], scope: str | None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Turn a parsed intent into a concrete spec + a blast-radius estimate."""
    store = workspace.store

    if op in ("rename", "inline_function"):
        row = workspace.find_symbol(params["symbol"])
        if row is None:
            raise LookupError(f"symbol {params['symbol']!r} is not in the index")
        spec = {"op": op, "file": row["file"], **params}
        try:
            impact = workspace.impact(row["symbol_id"])
            est = {
                "files": impact["counts"]["affected_files"] + 1,
                "symbols": impact["counts"]["affected_symbols"] + 1,
                "tests": impact["counts"]["affected_test_files"],
            }
        except Exception:  # noqa: BLE001
            est = {"files": 1, "symbols": 1, "tests": 0}
        return spec, est

    if op == "rename_parameter":
        row = workspace.find_symbol(params["symbol"])
        if row is None:
            raise LookupError(f"symbol {params['symbol']!r} is not in the index")
        return {"op": op, "file": row["file"], **params}, {"files": 1, "symbols": 1, "tests": 0}

    if op == "replace_api":
        pattern = f"{params['from']}($$$ARGS)"
        replacement = f"{params['to']}($$$ARGS)"
        try:
            from code_intelligence.core.context import structural

            discovery = structural.ast_match_files(
                store, workspace.root, pattern, None, scope, True, None
            )
            est = {
                "files": discovery["files_with_matches"],
                "symbols": discovery["total_matches"],
                "tests": 0,
            }
        except Exception:  # noqa: BLE001
            est = {"files": 0, "symbols": 0, "tests": 0}
        return {"op": op, "pattern": pattern, "replacement": replacement, "scope": scope}, est

    if op == "add_import":
        file = params.get("file") or (scope if scope and scope.endswith(".py") else None)
        if file is None:
            raise ValueError("add_import needs a target file — pass it as the scope")
        return {"op": op, "file": file, **params}, {"files": 1, "symbols": 0, "tests": 0}

    if op == "add_include":
        return {"op": op, **params}, {"files": 1, "symbols": 0, "tests": 0}

    if op in ("remove_unused_imports", "organize_imports", "remove_dead_code"):
        py = [
            r["path"] for r in store.list_files()
            if r["language"] == "python" and (scope is None or r["path"].startswith(scope))
        ]
        return {"op": op, "scope": scope}, {"files": len(py), "symbols": 0, "tests": 0}

    if op == "move_symbol":
        return {"op": op, **params}, {"files": 2, "symbols": 1, "tests": 0}

    if op in ("change_return_type", "change_parameter_type", "change_type", "forward_declare"):
        file = params.get("file")
        if file is None:
            row = workspace.find_symbol(params["symbol"])
            if row is None:
                raise LookupError(f"symbol {params['symbol']!r} is not in the index")
            file = row["file"]
        return {"op": op, **params, "file": file}, {"files": 1, "symbols": 1, "tests": 0}

    raise ValueError(f"planner does not know how to resolve op {op!r}")


def plan(workspace: Any, intent: str, scope: str | None = None) -> dict[str, Any]:
    """Resolve `intent` to an operation + estimate, and persist it as `plan_id`."""
    parsed = parse_intent(intent)
    if parsed["op"] == "unknown":
        return {"outcome": "not_understood", "understood": parsed["understood"]}

    spec, estimate = _resolve(workspace, parsed["op"], parsed["params"], scope)
    revision = workspace.store.get_revision("content")
    plan_id = _plan_id(intent, revision)
    workspace.store.plan_put(
        plan_id, intent, parsed["op"], json.dumps(spec), revision, json.dumps(estimate)
    )
    return {
        "plan_id": plan_id,
        "op": parsed["op"],
        "confidence": parsed["confidence"],
        "scope": scope,
        "estimate": estimate,
    }


# -- execute ------------------------------------------------------------


def _language_of(workspace: Any, spec: dict[str, Any]) -> str:
    """The language whose refactor backend should run this op."""
    for key in ("file", "from_file"):
        f = spec.get(key)
        if f:
            row = workspace.store.get_file(f)
            if row:
                return row["language"]
    scope = spec.get("scope")
    if scope and scope.endswith(".py"):
        return "python"
    return "python"


def _dispatch(workspace: Any, op: str, spec: dict[str, Any], commit: bool) -> dict[str, Any]:
    from code_intelligence.core.context import refactor_backends

    # replace_api / add_include are language-agnostic structural edits — keep
    # them on the direct path rather than per-language backends.
    if op == "replace_api":
        return workspace.ast_transform(
            spec["pattern"], spec["replacement"], scope=spec.get("scope"), commit=commit
        )
    if op == "add_include":
        return workspace.add_include(
            spec["file"], spec["include"], spec.get("system", False), commit=commit
        )
    language = _language_of(workspace, spec)
    backend = refactor_backends.get_backend(language)
    if backend is None:
        raise ValueError(f"no refactor backend for language {language!r}")
    if not backend.supports(op):
        return {
            "op": op,
            "outcome": "unsupported",
            "committed": False,
            "reason": f"the {language} backend ({backend.tool}) has no {op!r} primitive",
            "language": language,
        }
    result = backend.run(workspace, op, spec, commit)
    result.setdefault("op", op)
    return result


def execute(workspace: Any, plan_id: str, commit: bool = True) -> dict[str, Any]:
    """Run a previously `plan()`-ed operation; refuses if the workspace moved since."""
    row = workspace.store.plan_get(plan_id)
    if row is None:
        raise LookupError(f"no plan {plan_id!r}")
    if row["revision_content"] != workspace.store.get_revision("content"):
        return {
            "outcome": "stale_plan",
            "committed": False,
            "note": "the workspace changed since this plan was made — call plan() again",
        }
    result = _dispatch(workspace, row["op"], json.loads(row["spec_json"]), commit)
    result.setdefault("op", row["op"])
    return result


# -- task (plan + execute + repair) ----------------------------------


def _changed_files(result: dict[str, Any]) -> list[str]:
    files = result.get("changed_files") or result.get("files") or []
    return [f for f in files if isinstance(f, str)]


def _repair(workspace: Any, changed: list[str]) -> bool:
    """One deterministic repair pass over `changed` (ruff --fix + format). True if it changed anything."""
    from code_intelligence.core import exec as exec_module

    py = [f for f in changed if f.endswith(".py")]
    if not py:
        return False
    before = {}
    for f in py:
        p = workspace.root / f
        before[f] = p.read_text(encoding="utf-8", errors="replace") if p.exists() else None
    try:
        exec_module.run_ruff_fix(workspace.root, py)
    except Exception:  # noqa: BLE001 - no ruff, nothing to repair
        return False
    touched = any(
        (workspace.root / f).read_text(encoding="utf-8", errors="replace") != before[f]
        for f in py
        if (workspace.root / f).exists()
    )
    if touched:
        workspace.index()
    return touched


def task(
    workspace: Any,
    intent: str,
    scope: str | None = None,
    validate: bool = True,
    commit: bool = True,
    repair: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """State an intent; get back a compact outcome. plan -> execute -> (repair loop)."""
    planned = plan(workspace, intent, scope)
    if "plan_id" not in planned:
        return planned

    task_id = workspace.store.task_create(intent, scope)
    diags_before = len(workspace.get_violations("HIGH"))
    result = execute(workspace, planned["plan_id"], commit=commit)
    outcome = result.get("outcome", "committed" if result.get("committed") else "unknown")
    changed = _changed_files(result)

    repair_iterations = 0
    if commit and repair and repair.get("enabled") and result.get("committed") and changed:
        max_iters = min(repair.get("max_iterations", _MAX_REPAIR_ITERATIONS), _MAX_REPAIR_ITERATIONS)
        for _ in range(max_iters):
            if not _repair(workspace, changed):
                break
            repair_iterations += 1

    tests_run = tests_failed = 0
    validation = result.get("validation") or {}
    if isinstance(validation.get("tests"), dict):
        counts = validation["tests"].get("counts") or {}
        tests_run = sum(v for k, v in counts.items() if k in ("passed", "failed", "total")) or counts.get("total", 0)
        tests_failed = counts.get("failed", 0)

    diags_after = len(workspace.get_violations("HIGH"))

    summary = {
        "ok": bool(result.get("committed")) and outcome not in ("refused", "validation_failed", "apply_failed"),
        "operation": result.get("op") or planned["op"],
        "task_id": task_id,
        "plan_id": planned["plan_id"],
        "outcome": outcome,
        "files_changed": len(changed),
        "symbols_changed": result.get("changed_symbols", 0),
        "tests_run": tests_run,
        "tests_failed": tests_failed,
        "diagnostics_delta": diags_after - diags_before,
        "repair_iterations": repair_iterations,
    }
    for key in ("reason", "confidence", "failed_validation", "needs_manual",
                "impact", "warning", "escalation"):
        if key in result:
            summary[key] = result[key]
    workspace.store.task_finish(
        task_id, "done" if summary["ok"] else outcome, json.dumps(summary)
    )
    return summary


def resume_task(workspace: Any, task_id: str) -> dict[str, Any]:
    """Re-run a recorded task from its stored intent (re-derives plan+execute fresh)."""
    row = workspace.store.task_get(task_id)
    if row is None:
        raise LookupError(f"no task {task_id!r}")
    if row["status"] == "done":
        return {
            "task_id": task_id,
            "outcome": "already_done",
            "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
        }
    return task(workspace, row["intent"], row["scope"])


def task_status(workspace: Any, task_id: str) -> dict[str, Any]:
    """One recorded task's status + stored summary."""
    row = workspace.store.task_get(task_id)
    if row is None:
        raise LookupError(f"no task {task_id!r}")
    return {
        "task_id": task_id,
        "intent": row["intent"],
        "scope": row["scope"],
        "status": row["status"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "summary": json.loads(row["summary_json"]) if row["summary_json"] else None,
    }


def list_tasks(workspace: Any, limit: int = 20) -> dict[str, Any]:
    """Recent recorded tasks, newest first."""
    rows = workspace.store.task_list(limit)
    return {
        "tasks": [
            {"task_id": r["task_id"], "intent": r["intent"], "status": r["status"],
             "updated_at": r["updated_at"]}
            for r in rows
        ],
        "count": len(rows),
    }


# -- failure_context ---------------------------------------------------


def failure_context(workspace: Any, diagnostic_id: str, detail: str | None = None) -> dict[str, Any]:
    """The minimum context for one diagnose() entry; source only when `detail="source"`."""
    row = workspace.store.diagnostic_context_get(diagnostic_id)
    if row is None:
        raise LookupError(f"no diagnostic {diagnostic_id!r} (run diagnose() first)")

    out: dict[str, Any] = {
        "diagnostic_id": diagnostic_id,
        "kind": row["kind"],
        "message": row["message"],
        "file": row["file"],
        "line": row["line"],
    }
    if row["file"] and row["line"]:
        enclosing = None
        for sym in workspace.store.list_symbols_for_file(row["file"]):
            if sym["start_line"] <= row["line"] <= sym["end_line"] and (
                enclosing is None or sym["start_line"] >= enclosing["start_line"]
            ):
                enclosing = sym
        if enclosing:
            out["symbol"] = enclosing["qualified_name"] or enclosing["name"]
            out["range"] = [enclosing["start_line"], enclosing["end_line"]]
            out["callers"] = len(workspace.store.list_dependencies_to(enclosing["symbol_id"]))
            try:
                changed = workspace.change_summary()["changed_files"]
                out["changed_by_recent_edit"] = row["file"] in changed
            except Exception:  # noqa: BLE001, S110 - annotation is best-effort
                pass
            if detail == "source":
                src = workspace.read_range(
                    row["file"], enclosing["start_line"], enclosing["end_line"]
                )
                out["source"] = src.get("content")
    return out


# -- repository_summary (ENHANCEME §16) --------------------------------


def repository_summary(workspace: Any) -> dict[str, Any]:
    """A deterministic, compressed representation of the whole repository. No source."""
    store = workspace.store
    files = store.list_files()
    symbols = store.list_all_symbols()

    languages: dict[str, int] = {}
    tests = 0
    for f in files:
        languages[f["language"]] = languages.get(f["language"], 0) + 1
        if f["is_test_file"]:
            tests += 1

    public = sum(1 for s in symbols if not s["name"].startswith("_"))
    by_sev: dict[str, int] = {}
    from code_intelligence.core.diagnostics.violation import Severity

    for d in store.list_diagnostics():
        name = Severity(d["severity"]).name
        by_sev[name] = by_sev.get(name, 0) + 1

    ranked = workspace.rank_symbols(metric="cyclomatic_complexity", limit=10)
    hot = [
        {"symbol": r.get("name"), "file": r.get("file")}
        for r in ranked.get("items", [])
    ]

    recent = 0
    try:
        recent = len(workspace.change_summary()["changed_files"])
    except Exception:  # noqa: BLE001, S110 - summary works without a git repo
        pass

    return {
        "languages": dict(sorted(languages.items(), key=lambda kv: -kv[1])),
        "files": len(files),
        "symbols": len(symbols),
        "public_symbols": public,
        "test_files": tests,
        "diagnostics": dict(sorted(by_sev.items(), key=lambda kv: -Severity[kv[0]].value)),
        "uncommitted_changed_files": recent,
        "hot_symbols": hot,
        "revisions": workspace.revisions(),
    }


__all__ = [
    "execute",
    "failure_context",
    "list_tasks",
    "plan",
    "repository_summary",
    "resume_task",
    "task",
    "task_status",
]
