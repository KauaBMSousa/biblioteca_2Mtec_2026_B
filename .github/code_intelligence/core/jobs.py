"""Asynchronous jobs (ENHANCEME §11): run a long call in the background,
poll it, collect the result.

`task()` on a big repo, or `diagnose()` that runs the whole suite, can take
minutes. `job_start(method, params)` returns a `job_id` immediately; the
work runs on a worker thread and the agent polls `job_status` / collects
`job_result` when it is ready — or does something else in the meantime.

The worker executes through a *runner* the host installs with
`set_runner`: the daemon's runner takes the daemon's dispatch lock and
reuses its shared workspace (so a job is serialized against foreground
calls exactly as two foreground calls are); the default runner opens a
fresh `Workspace` for the job's root. One job runs at a time.
"""

import threading
import time
import uuid
from collections.abc import Callable
from typing import Any

_Runner = Callable[[str, dict[str, Any], str], Any]

_lock = threading.Lock()
_jobs: dict[str, dict[str, Any]] = {}
_runner: _Runner | None = None
_serialize = threading.Lock()  # one job body at a time

_MAX_RETAINED = 64


def set_runner(runner: _Runner) -> None:
    """Install the callable a worker uses to actually run `(method, params, root)`."""
    global _runner
    _runner = runner


def _default_runner(method: str, params: dict[str, Any], root: str) -> Any:
    from code_intelligence.core.workspace.workspace import Workspace
    from code_intelligence.daemon.protocol import dispatch

    with Workspace.open(root) as ws:
        return dispatch(ws, {"method": method, "params": params})


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _prune_locked() -> None:
    if len(_jobs) <= _MAX_RETAINED:
        return
    done = sorted(
        (j for j in _jobs.values() if j["status"] in ("done", "error", "cancelled")),
        key=lambda j: j["updated_at"],
    )
    for j in done[: len(_jobs) - _MAX_RETAINED]:
        _jobs.pop(j["job_id"], None)


def _work(job_id: str, method: str, params: dict[str, Any], root: str) -> None:
    with _lock:
        if _jobs[job_id]["status"] == "cancelled":
            return
        _jobs[job_id].update(status="running", updated_at=_now())
    runner = _runner or _default_runner
    with _serialize:
        try:
            value = runner(method, params, root)
            with _lock:
                if _jobs[job_id]["status"] != "cancelled":
                    _jobs[job_id].update(status="done", result=value, updated_at=_now())
        except Exception as exc:  # noqa: BLE001 - a failed job is a normal outcome to report
            with _lock:
                _jobs[job_id].update(
                    status="error",
                    error={"type": type(exc).__name__, "message": str(exc)},
                    updated_at=_now(),
                )


def start(method: str, params: dict[str, Any], root: str) -> dict[str, Any]:
    """Spawn a background worker for `method`; return `{job_id, status}` at once."""
    job_id = "j" + uuid.uuid4().hex[:12]
    with _lock:
        _jobs[job_id] = {
            "job_id": job_id,
            "method": method,
            "status": "queued",
            "result": None,
            "error": None,
            "created_at": _now(),
            "updated_at": _now(),
        }
        _prune_locked()
    threading.Thread(
        target=_work, args=(job_id, method, params, root), daemon=True
    ).start()
    return {"job_id": job_id, "status": "queued"}


def _get(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise LookupError(f"no job {job_id!r}")
        return dict(job)


def status(job_id: str) -> dict[str, Any]:
    """Liveness only — no result payload (use `job_result` for that)."""
    job = _get(job_id)
    job.pop("result", None)
    if job["status"] != "error":
        job.pop("error", None)
    return job


def result(job_id: str, wait: float | None = None) -> dict[str, Any]:
    """The job's outcome. `wait` blocks up to that many seconds for it to finish."""
    deadline = None if not wait else time.monotonic() + wait
    while True:
        job = _get(job_id)
        if job["status"] in ("done", "error", "cancelled"):
            return job
        if deadline is None or time.monotonic() >= deadline:
            return job
        time.sleep(0.1)


def cancel(job_id: str) -> dict[str, Any]:
    """Best-effort: a queued job never starts; a running job is flagged (its result is discarded)."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise LookupError(f"no job {job_id!r}")
        if job["status"] in ("queued", "running"):
            job.update(status="cancelled", updated_at=_now())
        return {"job_id": job_id, "status": job["status"]}


def list_jobs() -> dict[str, Any]:
    with _lock:
        rows = sorted(_jobs.values(), key=lambda j: j["created_at"], reverse=True)
        return {
            "jobs": [
                {"job_id": j["job_id"], "method": j["method"], "status": j["status"],
                 "updated_at": j["updated_at"]}
                for j in rows
            ],
            "count": len(rows),
        }


__all__ = ["cancel", "list_jobs", "result", "set_runner", "start", "status"]
