"""`git status`, structured: current branch, ahead/behind, staged/unstaged/untracked files.

Parses `git status --porcelain=v2 --branch` -- the one git status format
that is both machine-stable (unlike the human-porcelain default, which git
explicitly reserves the right to change) and carries the branch/upstream/
ahead-behind header in the same call, so one subprocess answers what would
otherwise take `git branch --show-current` + `git status` + `git
rev-list --count` as three.
"""

import subprocess
from pathlib import Path
from typing import Any

from code_intelligence.core.workspace.identity import STATE_DIR_NAME

#: XY status-code pairs %(index)(worktree) that mean the file has an
#: index-side (staged) or worktree-side (unstaged) change respectively.
_STAGED_CODES = frozenset("MADRC")
_UNSTAGED_CODES = frozenset("MADRCU")


def _is_own_state_dir(path: str) -> bool:
    """True for a path inside `<root>/.code-intelligence/` -- this tool's own
    index/socket/pidfile directory, not part of the repository a caller is
    asking about. Reported alongside `.git/` would be nonsensical; reported
    as an ordinary untracked/dirty path, it makes `git_status` lie about
    "is this workspace clean" purely because the tool itself was opened
    (which is what first creates the directory) -- so it is filtered the
    same way `.git/` already is invisible to plain `git status`.
    """
    return path == STATE_DIR_NAME or path.startswith(STATE_DIR_NAME + "/")


def git_status(root: Path) -> dict[str, Any]:
    """Current branch/upstream/ahead-behind plus staged/unstaged/untracked file lists.

    Raises `RuntimeError` when `root` is not inside a git repository --
    "not a git repo" and "a git repo with nothing changed" must never look
    alike to a caller deciding whether it is safe to run `git add`.
    """
    completed = subprocess.run(
        ["git", "status", "--porcelain=v2", "--branch"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"not a git repository (or git failed): {completed.stderr.strip()}")

    branch: str | None = None
    upstream: str | None = None
    ahead = 0
    behind = 0
    staged: list[str] = []
    unstaged: list[str] = []
    untracked: list[str] = []

    for line in completed.stdout.splitlines():
        if line.startswith("# branch.head "):
            head = line.removeprefix("# branch.head ")
            branch = None if head == "(detached)" else head
        elif line.startswith("# branch.upstream "):
            upstream = line.removeprefix("# branch.upstream ")
        elif line.startswith("# branch.ab "):
            parts = line.removeprefix("# branch.ab ").split()
            for part in parts:
                if part.startswith("+"):
                    ahead = int(part[1:])
                elif part.startswith("-"):
                    behind = int(part[1:])
        elif line.startswith("1 ") or line.startswith("2 "):
            # "1 XY ...  path" (ordinary) or "2 XY ... path\torig_path" (rename/copy).
            fields = line.split(" ", 8)
            xy = fields[1]
            path = fields[-1].split("\t", 1)[0]
            if _is_own_state_dir(path):
                continue
            index_status, worktree_status = xy[0], xy[1]
            if index_status in _STAGED_CODES:
                staged.append(path)
            if worktree_status in _UNSTAGED_CODES:
                unstaged.append(path)
        elif line.startswith("? "):
            path = line.removeprefix("? ")
            if not _is_own_state_dir(path.rstrip("/")):
                untracked.append(path)

    return {
        "branch": branch,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "staged": staged,
        "unstaged": unstaged,
        "untracked": untracked,
        "clean": not (staged or unstaged or untracked),
    }


__all__ = ["git_status"]
