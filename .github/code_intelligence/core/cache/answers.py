"""Revision-keyed cache for whole computed answers (ENHANCEME §N).

`impact(Foo::bar)`, `affected_tests(Foo::bar)`, `find_dependents(...)`,
`file_report(...)` are pure functions of the structural + semantic index.
Recomputing one on every call — a reverse-BFS over the dependency graph,
a coverage query — is wasted work whenever the index has not moved.

`cached()` keys each answer by `(method, params)` plus the `content` and
`semantic` revision counters it was computed at. A hit is returned only
when both counters still match; a stale row is ignored and later pruned.
Invalidation is therefore automatic and exact: any edit bumps `content`,
any `refresh_semantic` bumps `semantic`, and every dependent answer falls
out of cache at once.
"""

import hashlib
import json
from typing import Any, Callable

#: How far behind the current content revision a cached row may fall before
#: `cached()` opportunistically prunes it.
_PRUNE_LAG = 25


def _key(method: str, params: dict[str, Any]) -> str:
    payload = f"{method}:{json.dumps(params, sort_keys=True, default=str)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def cached(
    workspace: Any, method: str, params: dict[str, Any], compute: Callable[[], Any]
) -> Any:
    """Return the cached answer for `(method, params)` at the current revisions, or compute + store it.

    `compute` is only called on a miss. An exception from `compute`
    propagates and is never cached.
    """
    store = workspace.store
    revision_content = store.get_revision("content")
    revision_semantic = store.get_revision("semantic")
    key = _key(method, params)

    hit = store.answer_cache_get(key, revision_content, revision_semantic)
    if hit is not None:
        return json.loads(hit)

    value = compute()
    store.answer_cache_put(
        key, revision_content, revision_semantic, json.dumps(value, default=str)
    )
    if revision_content > _PRUNE_LAG:
        store.answer_cache_prune(revision_content - _PRUNE_LAG)
    return value


__all__ = ["cached"]
