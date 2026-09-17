"""Thin cache-invalidation policy used by `core/index/indexer.py`.

Per the plan: the index itself *is* the cache (a stored `files` row IS a
cached analysis result) — there is deliberately no separate cache
implementation duplicating what the index already stores, which is exactly
the "artificial duplication" fixme.md warns against. This module holds only
the *policy* of what counts as "still valid": a file's stored row is fresh
only when its `content_hash`, `parser_version` and `config_hash` all still
match the current run's values. Any mismatch — including a parser-logic
upgrade or a config edit that never touched the file itself — is a miss.
"""

from typing import Any


def is_fresh(stored_file_row: dict[str, Any] | None, content_hash: str, parser_version: str, config_hash: str) -> bool:
    """Return True when a stored `files` row is still valid for reuse (a cache hit).

    Args:
        stored_file_row: The file's current row from `IndexStore.get_file`,
            or None if it was never indexed (always a miss).
        content_hash: The file's current content hash.
        parser_version: The current run's `compute_parser_version()` value.
        config_hash: The current run's `compute_config_hash()` value.
    """
    if stored_file_row is None:
        return False
    return (
        stored_file_row["content_hash"] == content_hash
        and stored_file_row["parser_version"] == parser_version
        and stored_file_row["config_hash"] == config_hash
    )


__all__ = ["is_fresh"]
