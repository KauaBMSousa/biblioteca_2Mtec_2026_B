"""First-use workspace bootstrap: auto-writes the `code-intelligence` Claude Code skill."""

from code_intelligence.bootstrap.skill_writer import bootstrap_workspace, is_dotfiles_workspace

__all__ = ["bootstrap_workspace", "is_dotfiles_workspace"]
