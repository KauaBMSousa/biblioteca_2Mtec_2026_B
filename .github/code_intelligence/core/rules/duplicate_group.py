"""Defines DuplicateGroup, the union-find-merged duplication finding shape.

Ported unchanged (besides import paths — none needed here) from
`tools/code_quality/code_quality/rules/duplicate_group.py`.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class DuplicateGroup:
    """One union-find-merged group of mutually similar functions.

    Attributes:
        members: The functions in this group, each as a
            ``{"file", "function", "start_line", "end_line"}`` dict.
        similarity: The minimum pairwise similarity observed within the
            group (a conservative lower bound for the whole group).
    """

    members: list[dict] = field(default_factory=list)
    similarity: float = 0.0
