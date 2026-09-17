"""External static analyzers folded into the persistent index.

The `core.rules` modules answer structural questions from the parse tree
(too long, undocumented, duplicated). This package is for the other kind
of question — "does this code have a bug" — answered by a real analyzer
for the language: `cppcheck` for C++.

Each analyzer runs as a project-wide index pass (next to duplication and
test coverage), turns its findings into `Violation` rows under a stable
`<TOOL>_<id>` code, and from there they flow through `get_violations`,
`workspace_snapshot`, `diff` regressions and `context_for_task` like any
other diagnostic. Availability is reported, never guessed: a missing
analyzer skips its pass and says so.
"""

from code_intelligence.core.analysis import cppcheck

__all__ = ["cppcheck"]
