"""LOC / function-length threshold classification.

Extracted out of the old tool's `rules/loc.py` into its own module per the
plan's architecture table ("core/metrics/ — LOC/cyclomatic-complexity/
nesting-depth computation, ported from rules/loc.py's metric half"). The
threshold levels themselves come from fixme.md §1 (file LOC) and §2
(function length), documented there as engineering heuristics, not formal
quality properties — see `LOC_DISCLAIMER` in `reports/report_json.py`.

The actual complexity/nesting-depth *computation* stays inside
`core/parser/` (`_treesitter_common.py`/`python_adapter.py`), since it's
inherently coupled to each language's grammar node types — this module
only classifies an already-computed physical-line count against
configured thresholds, which is the language-agnostic, reusable half.
"""

from code_intelligence.core.diagnostics.violation import Severity

#: Threshold levels ordered from most to least severe: (threshold_key, code, severity).
LOC_LEVELS = [
    ("very_critical", "LOC_VERY_CRITICAL", Severity.VERY_CRITICAL),
    ("critical", "LOC_CRITICAL", Severity.CRITICAL),
    ("high", "LOC_HIGH", Severity.HIGH),
    ("warning", "LOC_WARNING", Severity.WARNING),
]
FUNCTION_LENGTH_LEVELS = [
    ("critical", "FUNCTION_LENGTH_CRITICAL", Severity.CRITICAL),
    ("high", "FUNCTION_LENGTH_HIGH", Severity.HIGH),
    ("warning", "FUNCTION_LENGTH_WARNING", Severity.WARNING),
]


def classify(physical_lines: int, thresholds: dict, levels: list[tuple[str, str, Severity]]) -> tuple[str, Severity] | None:
    """Map a line count to (code, severity) using descending threshold levels.

    `levels` must be ordered from most to least severe; the first level
    whose threshold is exceeded wins.
    """
    for threshold_key, code, severity in levels:
        if physical_lines > thresholds[threshold_key]:
            return code, severity
    return None


def physical_line_count(start_line: int, end_line: int) -> int:
    """Return the inclusive physical line count of a `[start_line, end_line]` span."""
    return end_line - start_line + 1


__all__ = ["LOC_LEVELS", "FUNCTION_LENGTH_LEVELS", "classify", "physical_line_count"]
