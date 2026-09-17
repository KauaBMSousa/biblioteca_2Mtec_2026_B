"""Cache invalidation policy — the index itself is the cache; see `invalidation.py`."""

from code_intelligence.core.cache.invalidation import is_fresh

__all__ = ["is_fresh"]
