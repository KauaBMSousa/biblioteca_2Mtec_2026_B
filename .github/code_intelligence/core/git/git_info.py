"""Best-effort git repository metadata for the JSON report's `repository` key.

Ported unchanged (besides import paths — none needed here) from
`tools/code_quality/code_quality/git_info.py`.
"""

import subprocess
from pathlib import Path


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Run a git subcommand, returning stripped stdout, or None on any failure."""
    try:
        result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def repository_info(root: str) -> dict[str, str | None]:
    """Best-effort git metadata for `root`: `{root, branch, commit}`, all None outside git."""
    root_path = Path(root)
    git_root = _run_git(["rev-parse", "--show-toplevel"], cwd=root_path)
    if git_root is None:
        return {"root": None, "branch": None, "commit": None}
    branch = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root_path)
    commit = _run_git(["rev-parse", "HEAD"], cwd=root_path)
    return {"root": git_root, "branch": branch, "commit": commit}


def resolve_ref(root: Path, ref: str) -> str | None:
    """Resolve any git-recognized ref (commit sha, branch, `HEAD~N`, tag, ...) to a commit sha."""
    return _run_git(["rev-parse", "--verify", "--quiet", ref], cwd=root)


__all__ = ["repository_info", "resolve_ref"]
