"""File-LOC and function-length threshold rules.

Thresholds come from fixme.md §1 (file LOC) and §2 (function length); both
are explicitly documented there as engineering heuristics, not formal
quality properties — see the disclaimer text emitted verbatim in the report
(`reports/report_json.py`/`reports/report_markdown.py`).

Ported from `tools/code_quality/code_quality/rules/loc.py`; the threshold
classification logic itself now lives in `core/metrics/thresholds.py` per
the plan's architecture table, imported here rather than duplicated.
"""

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.metrics.thresholds import FUNCTION_LENGTH_LEVELS, LOC_LEVELS, classify
from code_intelligence.core.parser.ir import FileAnalysis


def check_file_loc(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Check one file's physical LOC against `loc_thresholds`."""
    classification = classify(analysis.physical_lines, config["loc_thresholds"], LOC_LEVELS)
    if classification is None:
        return []
    code, severity = classification
    return [
        Violation(
            code=code,
            severity=severity,
            file=analysis.relpath,
            line=1,
            end_line=analysis.physical_lines,
            message=(
                f"{analysis.relpath} has {analysis.physical_lines} physical lines "
                f"(threshold breached: {code})"
            ),
            detail={"physical_lines": analysis.physical_lines},
            confidence="high",
        )
    ]


def check_function_lengths(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Check every function/method in one file against `function_length_thresholds`.

    Length is graded against complexity, not counted alone. Two 400-line
    functions are not the same finding:

        def build_frames(self):          void Backend::dispatch(...) {
            snap("step 1", ...)              if (a) { for (...) { if (b) {
            snap("step 2", ...)                  ... 40 nested branches ...
            ... 200 more calls ...

    The first is a table written as code: one straight-line path,
    cyclomatic complexity 1, nothing to hold in your head at once. The
    second is where length actually costs something. Reporting both as
    CRITICAL teaches the reader to ignore the rule, so a function whose
    complexity is at or below ``function_length_thresholds
    .low_complexity_downgrade`` drops one severity level and says why.
    Still reported -- long is long, and a 400-line table may well want
    splitting -- just not as the same emergency.
    """
    violations: list[Violation] = []
    thresholds = config["function_length_thresholds"]
    complexity_floor = thresholds.get("low_complexity_downgrade", 0)

    all_functions = list(analysis.top_level_functions)
    for cls in analysis.classes:
        all_functions.extend(cls.methods)

    for func in all_functions:
        classification = classify(func.physical_lines, thresholds, FUNCTION_LENGTH_LEVELS)
        if classification is None:
            continue
        code, severity = classification
        complexity = func.cyclomatic_complexity
        message = (
            f"{func.qualified_name} has {func.physical_lines} physical lines "
            f"(threshold breached: {code})"
        )
        detail = {"function": func.qualified_name, "physical_lines": func.physical_lines}
        if complexity is not None:
            detail["cyclomatic_complexity"] = complexity
        if (
            complexity_floor
            and complexity is not None
            and complexity <= complexity_floor
            and severity > Severity.WARNING
        ):
            severity = Severity(severity - 1)
            message += (
                f", but its cyclomatic complexity is {complexity}: the length looks "
                "inherent (a table, a sequence of declarations), not control flow"
            )
            detail["severity_downgraded"] = True
        violations.append(
            Violation(
                code=code,
                severity=severity,
                file=analysis.relpath,
                line=func.start_line,
                end_line=func.end_line,
                message=message,
                detail=detail,
                column=func.start_column,
                end_column=func.end_column,
                confidence="high",
            )
        )
    return violations


def check(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Run all LOC-related checks (file LOC + function length) on one file."""
    return check_file_loc(analysis, config) + check_function_lengths(analysis, config)
