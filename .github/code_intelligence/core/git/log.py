"""`git log`, structured: commit history for a repo or one path.

A caller asking "who last touched this and when" otherwise has to shell
out to `git log` and parse whatever format its own git version's default
happens to be -- this pins an explicit `--pretty=format:` using control
characters as field/record separators (never legal inside a commit
subject) so the parse is exact, not line-splitting on ": " and hoping.
"""

import subprocess
from pathlib import Path
from typing import Any

_FIELD_SEP = "\x1f"
_RECORD_SEP = "\x1e"
_FORMAT = _FIELD_SEP.join(["%H", "%h", "%an", "%ae", "%ad", "%s"])


def git_log(root: Path, path: str | None = None, ref: str = "HEAD", limit: int = 20) -> list[dict[str, Any]]:
    """Commit history for `ref` (default `HEAD`), optionally scoped to one `path`.

    Raises `RuntimeError` outside a git repository or for an unknown `ref`
    -- same "a query result must never look like success" rule
    `core.git.status.git_status` follows.
    """
    args = [
        "git", "log", f"--pretty=format:{_FORMAT}{_RECORD_SEP}", "--date=iso-strict",
        f"-n{limit}", ref,
    ]
    if path:
        args += ["--", path]
    completed = subprocess.run(args, cwd=root, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        raise RuntimeError(f"git log failed: {completed.stderr.strip()}")

    commits: list[dict[str, Any]] = []
    for record in completed.stdout.split(_RECORD_SEP):
        record = record.strip("\n")
        if not record:
            continue
        sha, short_sha, author_name, author_email, date, subject = record.split(_FIELD_SEP)
        commits.append(
            {
                "sha": sha,
                "short_sha": short_sha,
                "author_name": author_name,
                "author_email": author_email,
                "date": date,
                "subject": subject,
            }
        )
    return commits


__all__ = ["git_log"]
