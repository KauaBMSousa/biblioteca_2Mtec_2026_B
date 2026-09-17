"""Content hashing for symbols, range queries and patch staleness checks.

Deliberately separate from the blake2b ``file_hash`` used for cache
invalidation (`core/index/indexer.py`, `core/parser/*`) — that hash is
tested, working, and serves a different purpose (whole-file cache keys).
This module's ``content_hash`` is independently reproducible by an agent
via plain ``sha256sum`` on the exact byte range it was computed from, which
is the property needed for stale-context protection in `core/patches/`.

Ported unchanged from `tools/code_quality/code_quality/content_hash.py`.
"""

import hashlib


def content_hash(text: str) -> str:
    """Return a ``"sha256:" + hexdigest`` hash of `text`.

    Args:
        text: The exact text span to hash (e.g. one symbol's source, or an
            arbitrary line range read from disk).

    Returns:
        A string of the form ``"sha256:<64 hex chars>"``, reproducible by
        running ``sha256sum`` on the identical UTF-8 byte range.
    """
    digest = hashlib.sha256(text.encode("utf-8", errors="replace")).hexdigest()
    return f"sha256:{digest}"
