"""One-type-per-file and file/class name matching rules (fixme.md §4.3/§4.4).

- FILE_CLASS_MISMATCH: when a file contains exactly one "main" type
  (class/struct/interface), the file's base name must match the type's name
  (case-insensitively, normalized by stripping underscores/hyphens so
  language naming-convention differences like `Widget.py` vs `widget_test`
  don't produce false positives).
- MULTIPLE_TYPE_DEFINITION: more than one class/struct/interface defined in
  a single file. This is a strict, zero-exception rule.

Ported unchanged (besides import paths) from
`tools/code_quality/code_quality/rules/structure.py`.
"""

from pathlib import Path

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.parser.ir import ClassInfo, FileAnalysis


def _normalize(name: str) -> str:
    """Normalize a name for case/underscore/hyphen-insensitive comparison."""
    return name.lower().replace("_", "").replace("-", "")


def check_file_class_match(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Flag a mismatch between the file's base name and its single main type's name."""
    if not config["rules"].get("file_class_name_match"):
        return []
    if len(analysis.classes) != 1:
        return []  # ambiguous with 0 or >1 types; MULTIPLE_TYPE_DEFINITION covers >1

    cls = analysis.classes[0]
    stem = Path(analysis.relpath).stem
    if _normalize(stem) == _normalize(cls.name):
        return []

    return [
        Violation(
            code="FILE_CLASS_MISMATCH",
            severity=Severity.WARNING,
            file=analysis.relpath,
            line=cls.start_line,
            end_line=cls.start_line,
            message=(
                f"File '{analysis.relpath}' does not match its {cls.kind} name "
                f"'{cls.name}'"
            ),
            detail={"file_stem": stem, "type_name": cls.name},
            column=cls.start_column,
            end_column=cls.end_column,
            confidence="high",
        )
    ]


def check_single_type_per_file(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Flag more than one class/struct/interface defined in one file."""
    if not config["rules"].get("single_class_per_file"):
        return []
    if len(analysis.classes) <= 1:
        return []

    names = ", ".join(f"{c.kind} {c.name}" for c in analysis.classes)
    first: ClassInfo = analysis.classes[0]
    return [
        Violation(
            code="MULTIPLE_TYPE_DEFINITION",
            severity=Severity.HIGH,
            file=analysis.relpath,
            line=first.start_line,
            end_line=analysis.classes[-1].end_line,
            message=(
                f"File '{analysis.relpath}' defines {len(analysis.classes)} types "
                f"in one file: {names}"
            ),
            detail={"types": [{"name": c.name, "kind": c.kind, "line": c.start_line} for c in analysis.classes]},
            column=first.start_column,
            end_column=analysis.classes[-1].end_column,
            confidence="high",
        )
    ]


def check(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Run both structural checks (file/class match + one-type-per-file) on one file."""
    return check_file_class_match(analysis, config) + check_single_type_per_file(analysis, config)
