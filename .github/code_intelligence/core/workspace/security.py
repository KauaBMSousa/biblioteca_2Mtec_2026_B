"""Shared path-containment guard (fixme §37).

Every path in every query — CLI, daemon, MCP, LSP alike — must be resolved
and checked to be inside the currently-open workspace's root before any
file read. This is the single shared implementation; callers must not
reimplement path resolution/containment locally.

Rejects with a clear, typed error — never silently clamps a traversal
attempt into "the nearest valid path", and never serves the file.
"""

from pathlib import Path


class PathTraversalError(ValueError):
    """A workspace-relative path argument resolved outside the workspace root."""


def resolve_within_workspace(root: Path, relpath: str) -> Path:
    """Resolve `relpath` against `root`, guaranteeing containment.

    Accepts both a workspace-relative path and an absolute path (the
    latter is still checked for containment — an absolute path elsewhere
    on disk is exactly the traversal case this guards against). Symlinks
    are resolved before the containment check, so a symlink inside the
    workspace that points outside it is also rejected.

    Args:
        root: The open workspace's resolved root.
        relpath: A path argument taken from a query (untrusted).

    Returns:
        The resolved, contained, absolute path.

    Raises:
        PathTraversalError: `relpath` resolves outside `root`.
    """
    root_resolved = root.resolve()
    candidate = Path(relpath)
    combined = candidate if candidate.is_absolute() else root_resolved / candidate
    resolved = combined.resolve()
    try:
        resolved.relative_to(root_resolved)
    except ValueError:
        raise PathTraversalError(
            f"path {relpath!r} resolves outside workspace root {root_resolved} (got {resolved})"
        ) from None
    return resolved


__all__ = ["PathTraversalError", "resolve_within_workspace"]
