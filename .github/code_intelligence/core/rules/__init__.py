"""Rule modules: naming, docstrings, structure, procedural, generated, duplication, parse_fallback.

Ported from `tools/code_quality/code_quality/rules/`. Every per-file rule
module exposes a `check(analysis, config) -> list[Violation]` function;
`duplication` additionally exposes `detect`/`check` at fileset scope.
`core/index/indexer.py` is the caller that wires these to the persistent
index instead of an in-memory `list[FileAnalysis]`.
"""

from code_intelligence.core.diagnostics.violation import Violation
from code_intelligence.core.parser.ir import FileAnalysis
from code_intelligence.core.rules import (
    docstrings,
    embedded_listing,
    generated,
    loc,
    naming,
    parse_fallback,
    procedural,
    structure,
)
from code_intelligence.core.rules.duplicate_group import DuplicateGroup
from code_intelligence.core.rules.duplication import check as check_duplication
from code_intelligence.core.rules.duplication import detect as detect_duplication

__all__ = [
    "docstrings",
    "embedded_listing",
    "generated",
    "loc",
    "naming",
    "parse_fallback",
    "procedural",
    "structure",
    "DuplicateGroup",
    "check_duplication",
    "detect_duplication",
    "run_file_rules",
]


def run_file_rules(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Run every per-file (non-cross-file) rule module on one FileAnalysis.

    Excludes `duplication`, which is fileset-scoped and run separately by
    the indexer once every file in the affected set has been (re)parsed.
    """
    violations: list[Violation] = []
    violations += loc.check(analysis, config)
    violations += naming.check(analysis, config)
    violations += docstrings.check(analysis, config)
    violations += structure.check(analysis, config)
    violations += procedural.check(analysis, config)
    violations += parse_fallback.check(analysis, config)
    violations += embedded_listing.check(analysis, config)
    return violations
