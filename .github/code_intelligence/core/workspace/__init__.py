"""`Workspace`: the public API tying together index/parser/symbols/rules/context/git."""

from code_intelligence.core.workspace.identity import compute_workspace_id, resolve_root
from code_intelligence.core.workspace.workspace import Workspace

__all__ = ["Workspace", "compute_workspace_id", "resolve_root"]
