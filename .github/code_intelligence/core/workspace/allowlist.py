"""Daemon workspace allowlist (fixme §37) — first bootstrap use IS the authorization.

`~/.config/code-intelligence/allowed_workspaces.json` is a JSON array of
resolved workspace root paths the **daemon** is permitted to open (the
in-process CLI/MCP/LSP fallback path does not consult this file — it's
opened directly by the invoking user against a root they named; the
allowlist specifically gates the daemon, which is the multi-workspace-
capable, longer-lived component). It is populated the moment
`bootstrap/skill_writer.py` successfully bootstraps a workspace for the
first time — never by hand, never proactively.

The path is overridable via `CODE_INTELLIGENCE_ALLOWLIST_PATH` so tests
never touch a real user's `~/.config/`; `code_intelligence`'s own test
suite sets this for every test via an autouse fixture in `tests/conftest.py`.
"""

import json
import os
from pathlib import Path

_ENV_VAR = "CODE_INTELLIGENCE_ALLOWLIST_PATH"
_DEFAULT_PATH = Path.home() / ".config" / "code-intelligence" / "allowed_workspaces.json"


def allowlist_path() -> Path:
    """The effective allowlist file path — `$CODE_INTELLIGENCE_ALLOWLIST_PATH` or the default."""
    override = os.environ.get(_ENV_VAR)
    return Path(override) if override else _DEFAULT_PATH


def _load(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []


def list_allowed() -> list[str]:
    """Every resolved workspace root path currently on the allowlist."""
    return _load(allowlist_path())


def is_allowed(root: Path) -> bool:
    """True when `root` (resolved) is present on the allowlist."""
    return str(root.resolve()) in _load(allowlist_path())


def authorize(root: Path) -> None:
    """Idempotently add `root` to the allowlist, creating the file/dir as needed."""
    path = allowlist_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    entries = _load(path)
    resolved = str(root.resolve())
    if resolved not in entries:
        entries.append(resolved)
        path.write_text(json.dumps(sorted(entries), indent=2) + "\n", encoding="utf-8")


__all__ = ["allowlist_path", "list_allowed", "is_allowed", "authorize"]
