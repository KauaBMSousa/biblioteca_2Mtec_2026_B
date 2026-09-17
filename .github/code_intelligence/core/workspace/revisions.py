"""Cheap stale-state detection: the workspace's monotonic revision tokens (ENHANCEME §O).

Instead of an agent re-hashing files or re-running `status` to find out
whether a cached answer still holds, every mutating path bumps a small
integer counter and this module reports them together. The contract:

* `content` increments on every local edit (`replace_symbol`,
  `insert_lines`, applied `rename_symbol`, applied `ast_replace`) and on
  every index pass that reparsed or dropped a file.
* `indexed` is set equal to `content` at the end of each index pass.
* `semantic` increments whenever `refresh_semantic` re-extracted a unit.

So `index_fresh` is simply `content == indexed`, and a caller holding a
result computed at `semantic == N` knows it is stale the moment the
reported `semantic` moves past `N`.
"""

from pathlib import Path
from typing import Any

from code_intelligence.core.git.git_info import resolve_ref
from code_intelligence.core.index.store import IndexStore


def revisions(store: IndexStore, root: Path) -> dict[str, Any]:
    """The `{content, indexed, semantic, git, index_fresh}` revision token block."""
    content = store.get_revision("content")
    indexed = store.get_revision("indexed")
    return {
        "content": content,
        "indexed": indexed,
        "semantic": store.get_revision("semantic"),
        "git": resolve_ref(root, "HEAD"),
        "index_fresh": content == indexed,
    }


__all__ = ["revisions"]
