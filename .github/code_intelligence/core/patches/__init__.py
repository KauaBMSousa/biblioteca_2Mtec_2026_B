"""Index-backed, hash-gated, scope-validated patch application."""

from code_intelligence.core.patches.patch import (
    EXIT_OK,
    EXIT_PATCH_FAILS,
    EXIT_SCOPE_VIOLATION,
    EXIT_STALE_CONTEXT,
    EXIT_USAGE_ERROR,
    PatchResult,
    apply_patch,
)

__all__ = [
    "apply_patch",
    "PatchResult",
    "EXIT_OK",
    "EXIT_USAGE_ERROR",
    "EXIT_STALE_CONTEXT",
    "EXIT_PATCH_FAILS",
    "EXIT_SCOPE_VIOLATION",
]
