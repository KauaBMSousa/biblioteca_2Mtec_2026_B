"""Stable workspace identity and on-disk state-directory conventions.

`workspace_id` is a sha256 of the resolved, symlink-free absolute root
path — stable across relative-path invocations, changes only if the
workspace is physically moved. All of a workspace's own state (SQLite
index, and in Phase B the daemon socket/pidfile) lives in
`<root>/.code-intelligence/`, analogous to `.git/`.
"""

import hashlib
from pathlib import Path

#: The state directory name, added to a workspace's `.gitignore` by
#: `bootstrap/skill_writer.py` on first use.
STATE_DIR_NAME = ".code-intelligence"

#: The SQLite index filename within the state directory.
INDEX_DB_NAME = "index.sqlite3"


def resolve_root(root: str | Path) -> Path:
    """Resolve `root` to an absolute, symlink-free path (the identity basis)."""
    return Path(root).resolve()


def compute_workspace_id(root: Path) -> str:
    """Sha256 hex digest of the resolved root path — this workspace's stable identity."""
    return hashlib.sha256(str(root).encode("utf-8")).hexdigest()


def state_dir(root: Path) -> Path:
    """The `.code-intelligence/` state directory for `root` (may not exist yet)."""
    return root / STATE_DIR_NAME


def index_db_path(root: Path) -> Path:
    """The SQLite index file path for `root` (may not exist yet)."""
    return state_dir(root) / INDEX_DB_NAME


__all__ = ["STATE_DIR_NAME", "INDEX_DB_NAME", "resolve_root", "compute_workspace_id", "state_dir", "index_db_path"]
