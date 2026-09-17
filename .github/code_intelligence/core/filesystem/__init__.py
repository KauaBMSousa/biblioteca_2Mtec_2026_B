"""Gitignore-aware file discovery, ported from `tools/code_quality/code_quality/discovery.py`."""

from code_intelligence.core.filesystem.discovery import (
    DiscoveryResult,
    discover_all,
    discover_changed,
    discover_changed_hunks,
    is_git_repo,
    resolve_base_ref,
)
from code_intelligence.core.filesystem.discovery_error import DiscoveryError

__all__ = [
    "DiscoveryError",
    "DiscoveryResult",
    "discover_all",
    "discover_changed",
    "discover_changed_hunks",
    "is_git_repo",
    "resolve_base_ref",
]
