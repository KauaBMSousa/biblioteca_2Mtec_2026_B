"""Git plumbing: repository metadata + regression/ref-based diff comparison."""

from code_intelligence.core.git.diff_report import (
    STAGED,
    WORKING_TREE,
    changed_files_for,
    compare,
    content_at,
    symbol_diff,
    violation_diff,
)
from code_intelligence.core.git.git_info import repository_info, resolve_ref

__all__ = [
    "repository_info",
    "resolve_ref",
    "compare",
    "changed_files_for",
    "content_at",
    "symbol_diff",
    "violation_diff",
    "WORKING_TREE",
    "STAGED",
]
