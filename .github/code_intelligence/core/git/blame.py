"""`git blame`, structured: which commit last touched each line of a file (or range).

Parses `--porcelain` -- the one blame format git documents as stable,
unlike the human-readable default -- rather than the column-aligned
default output, whose author-name width depends on the widest name in the
file being blamed.
"""

import subprocess
from pathlib import Path
from typing import Any

_HEX_DIGITS = frozenset("0123456789abcdef")


def _is_commit_header(token: str) -> bool:
    """True for a porcelain group header's leading sha (40 lowercase hex chars)."""
    return len(token) == 40 and all(c in _HEX_DIGITS for c in token)


def git_blame(
    root: Path, file: str, start_line: int | None = None, end_line: int | None = None
) -> list[dict[str, Any]]:
    """Per-line blame for `file`, optionally restricted to `[start_line, end_line]` (1-based, inclusive).

    Raises `RuntimeError` when `file` is not tracked, does not exist, or
    `root` is not a git repository.
    """
    args = ["git", "blame", "--porcelain"]
    if start_line is not None and end_line is not None:
        args += ["-L", f"{start_line},{end_line}"]
    args += ["--", file]
    completed = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"git blame failed: {completed.stderr.strip()}")

    lines: list[dict[str, Any]] = []
    commit_meta: dict[str, dict[str, str]] = {}
    current_sha: str | None = None
    current_line_no: int | None = None

    for raw in completed.stdout.splitlines():
        if raw.startswith("\t"):
            meta = commit_meta.get(current_sha, {})
            lines.append(
                {
                    "line": current_line_no,
                    "sha": current_sha,
                    "author": meta.get("author"),
                    "author_email": (meta.get("author-mail") or "").strip("<>") or None,
                    "author_time": meta.get("author-time"),
                    "summary": meta.get("summary"),
                    "content": raw[1:],
                }
            )
            continue

        head, _, rest = raw.partition(" ")
        if _is_commit_header(head):
            fields = raw.split()
            current_sha = fields[0]
            current_line_no = int(fields[2])
            commit_meta.setdefault(current_sha, {})
        elif current_sha is not None and rest:
            commit_meta[current_sha][head] = rest

    return lines


__all__ = ["git_blame"]
