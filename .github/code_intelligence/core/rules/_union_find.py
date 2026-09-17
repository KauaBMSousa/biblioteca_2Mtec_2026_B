"""Minimal union-find (disjoint-set) helper used by duplication detection.

Ported unchanged from `tools/code_quality/code_quality/rules/_union_find.py`.
"""


class _UnionFind:
    """Minimal union-find for merging transitively-connected duplicate edges."""

    def __init__(self) -> None:
        """Start with every element as its own singleton set."""
        self.parent: dict[int, int] = {}

    def find(self, item: int) -> int:
        """Return the representative (root) of the set containing `item`."""
        self.parent.setdefault(item, item)
        while self.parent[item] != item:
            self.parent[item] = self.parent[self.parent[item]]
            item = self.parent[item]
        return item

    def union(self, left: int, right: int) -> None:
        """Merge the sets containing `left` and `right`."""
        root_left, root_right = self.find(left), self.find(right)
        if root_left != root_right:
            self.parent[root_left] = root_right
