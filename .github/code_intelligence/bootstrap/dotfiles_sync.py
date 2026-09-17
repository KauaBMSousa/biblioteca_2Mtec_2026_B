"""The real dotfiles bootstrap branch (Phase B migration step 6e).

`skill_writer.bootstrap_workspace()` imports `sync_dotfiles_skill` from
here — lazily, and only once this module exists — the moment it detects
`root` is dotfiles itself (via the `scripts/dev/sync_cross_project_skills.sh`
marker). Before this module existed, that branch was a documented no-op
(Phase A); this is the real implementation, wired in last, after
`tools/code_quality/` was already retired and the new
`.claude/commands/code-intelligence.md` authored by hand (this module
does not author that file's *content* — it only confirms it is present
and re-runs the sync script, matching the plan's "write/confirm
`.claude/commands/code-intelligence.md` is current + optionally re-run the
sync script" instruction).
"""

import subprocess
from pathlib import Path

_SYNC_SCRIPT = "scripts/dev/sync_cross_project_skills.sh"
_SKILL_COMMAND = ".claude/commands/code-intelligence.md"


def sync_dotfiles_skill(root: Path) -> dict[str, object]:
    """Confirm the `code-intelligence` command exists, then re-run dotfiles' own sync script.

    Returns a small report dict for logging/tests. Never writes
    `.claude/commands/code-intelligence.md`'s *content* — that file is
    hand-authored (see the plan's migration step 6d) and only confirmed
    present here; this function's job is exclusively to keep the two
    generated mirrors (`.github/skills/`, `stow/.../opencode/skills/`) in
    sync with it, the same way every other `.claude/commands/*.md` file is
    kept in sync.
    """
    command_path = root / _SKILL_COMMAND
    if not command_path.is_file():
        return {
            "workspace": "dotfiles",
            "action": "error",
            "detail": f"{_SKILL_COMMAND} is missing — author it by hand first (migration step 6d)",
        }

    script_path = root / _SYNC_SCRIPT
    result = subprocess.run(
        ["bash", str(script_path)], cwd=root, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        return {
            "workspace": "dotfiles",
            "action": "sync_failed",
            "detail": result.stderr.strip() or result.stdout.strip(),
        }

    return {
        "workspace": "dotfiles",
        "action": "synced",
        "command_path": str(command_path),
        "sync_output": result.stdout.strip(),
    }


__all__ = ["sync_dotfiles_skill"]
