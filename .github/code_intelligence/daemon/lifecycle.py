"""`daemon start|status|stop` process management via a pidfile.

Never invoked automatically — `daemon.autostart` stays `false` by default
(Phase A already reserved this config key); this module is only exercised
by the explicit `code-intelligence daemon ...` CLI subcommand, or directly
by tests.

`start()` spawns `_entrypoint.py` via `subprocess.Popen(...,
start_new_session=True)` — detached from the calling terminal/session (the
daemonize step) — then polls for the pidfile + a live socket ping before
declaring success, so a workspace that fails the allowlist check (or any
other startup error) is reported as a failure, not a false "started".
`stop()` sends `SIGTERM` and waits for the socket file to disappear,
falling back to `SIGKILL` if the process ignores it. `status()` checks pid
liveness and, if alive, pings the real socket and folds in the daemon's
own observability metrics (fixme §39).
"""

import json
import os
import signal
import socket
import sys
import time
from pathlib import Path
from typing import Any

from code_intelligence.core.workspace import identity

_ENTRYPOINT = Path(__file__).resolve().parent / "_entrypoint.py"
_POLL_INTERVAL = 0.05


def _pid_path(root: Path) -> Path:
    return identity.state_dir(root) / "daemon.pid"


def _sock_path(root: Path) -> Path:
    return identity.state_dir(root) / "daemon.sock"


def _log_path(root: Path) -> Path:
    return identity.state_dir(root) / "daemon.log"


def _read_pid(root: Path) -> int | None:
    pid_path = _pid_path(root)
    if not pid_path.is_file():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just not ours to signal-probe further
    return True


def ping(root: Path, timeout: float = 2.0) -> dict[str, Any] | None:
    """Send a `ping` request over the real socket; `None` if unreachable."""
    sock_path = _sock_path(root)
    if not sock_path.exists():
        return None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(sock_path))
            sock.sendall((json.dumps({"id": 1, "method": "ping"}) + "\n").encode("utf-8"))
            buffer = b""
            while b"\n" not in buffer:
                chunk = sock.recv(65536)
                if not chunk:
                    return None
                buffer += chunk
            return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))
    except OSError:
        return None


def call(root: Path, method: str, params: dict | None = None, timeout: float = 10.0) -> dict[str, Any]:
    """Send one request to a running daemon's socket and return its raw response envelope.

    Raises `ConnectionError` if the socket is not reachable — callers that
    want the daemon-or-in-process fallback should catch this.
    """
    sock_path = _sock_path(root)
    if not sock_path.exists():
        raise ConnectionError(f"no daemon socket at {sock_path}")
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        try:
            sock.connect(str(sock_path))
        except OSError as exc:
            raise ConnectionError(f"could not connect to {sock_path}: {exc}") from exc
        sock.sendall((json.dumps({"id": 1, "method": method, "params": params or {}}) + "\n").encode("utf-8"))
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(65536)
            if not chunk:
                raise ConnectionError("daemon closed the connection without responding")
            buffer += chunk
        return json.loads(buffer.split(b"\n", 1)[0].decode("utf-8"))


def _daemonize_and_exec(root: Path) -> None:
    """Classic double-fork: the grandchild (the real daemon) is reparented to init
    immediately, so it is never *this calling process's* zombie to reap — no matter
    how long the caller itself stays alive afterwards (matters for tests, which run
    `start()` and `stop()` from the same long-lived pytest process).
    """
    first_pid = os.fork()
    if first_pid == 0:
        os.setsid()
        second_pid = os.fork()
        if second_pid > 0:
            os._exit(0)  # first child: done, let the grandchild be orphaned/reparented

        # Grandchild: detach stdio, close inherited fds, exec into the daemon entrypoint.
        try:
            max_fd = os.sysconf("SC_OPEN_MAX")
        except (ValueError, OSError):
            max_fd = 256
        for fd in range(3, max_fd):
            try:
                os.close(fd)
            except OSError:
                pass
        devnull_fd = os.open(os.devnull, os.O_RDWR)
        os.dup2(devnull_fd, 0)
        os.dup2(devnull_fd, 1)
        os.dup2(devnull_fd, 2)
        try:
            os.execv(sys.executable, [sys.executable, str(_ENTRYPOINT), "--root", str(root)])
        except OSError:
            os._exit(1)  # pragma: no cover - only reachable if exec itself fails
        os._exit(1)  # pragma: no cover - unreachable; execv never returns on success
    else:
        os.waitpid(first_pid, 0)  # reap the first child immediately - never a lingering zombie


def start(root: Path, timeout: float = 10.0) -> dict[str, Any]:
    """Start the daemon for `root` if not already running; wait for it to come up."""
    root = identity.resolve_root(root)
    existing_pid = _read_pid(root)
    if existing_pid is not None and _pid_alive(existing_pid) and ping(root) is not None:
        return {"status": "already_running", "pid": existing_pid, "socket": str(_sock_path(root))}

    identity.state_dir(root).mkdir(parents=True, exist_ok=True)
    _daemonize_and_exec(root)

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        response = ping(root)
        if response is not None and "result" in response:
            return {"status": "started", "pid": _read_pid(root), "socket": str(_sock_path(root))}
        time.sleep(_POLL_INTERVAL)

    log_tail = ""
    log_path = _log_path(root)
    if log_path.is_file():
        log_tail = "\n".join(log_path.read_text(encoding="utf-8").splitlines()[-20:])
    return {"status": "failed", "detail": "daemon did not become reachable in time", "log_tail": log_tail}


def stop(root: Path, timeout: float = 10.0) -> dict[str, Any]:
    """Stop the daemon for `root`, if running; waits for clean socket/pidfile removal."""
    root = identity.resolve_root(root)
    pid = _read_pid(root)
    if pid is None or not _pid_alive(pid):
        _sock_path(root).unlink(missing_ok=True)
        _pid_path(root).unlink(missing_ok=True)
        return {"status": "not_running"}

    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _sock_path(root).exists() and not _pid_alive(pid):
            return {"status": "stopped", "pid": pid}
        time.sleep(_POLL_INTERVAL)

    # Didn't shut down cleanly in time - force it, then confirm.
    if _pid_alive(pid):
        os.kill(pid, signal.SIGKILL)
        time.sleep(0.2)
    _sock_path(root).unlink(missing_ok=True)
    _pid_path(root).unlink(missing_ok=True)
    return {"status": "killed", "pid": pid}


def status(root: Path) -> dict[str, Any]:
    """Report whether the daemon is running, and — if reachable — its live metrics."""
    root = identity.resolve_root(root)
    pid = _read_pid(root)
    if pid is None or not _pid_alive(pid):
        return {"status": "not_running"}

    response = call(root, "status") if _sock_path(root).exists() else None
    if response is None or "result" not in response:
        return {"status": "unresponsive", "pid": pid}

    return {"status": "running", "pid": pid, "socket": str(_sock_path(root)), "workspace_status": response["result"]}


__all__ = ["start", "stop", "status", "ping", "call"]
