"""Mandatory documentation rule (fixme.md §4.1): MISSING_DOCSTRING.

Every function, method and class must have an associated documentation
comment (docstring/Javadoc/JSDoc/PHPDoc/`/** */`) immediately preceding (or,
for Python, opening) its definition.

Ported unchanged (besides import paths) from
`tools/code_quality/code_quality/rules/docstrings.py`.
"""

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.parser.ir import ClassInfo, FileAnalysis, FunctionInfo


def _missing_doc_violation(relpath: str, line: int, name: str, kind: str, column: int, end_column: int) -> Violation:
    """Build one MISSING_DOCSTRING violation for a class/method/function."""
    return Violation(
        code="MISSING_DOCSTRING",
        severity=Severity.WARNING,
        file=relpath,
        line=line,
        end_line=line,
        message=f"{kind} '{name}' has no documentation comment",
        detail={"name": name, "kind": kind},
        column=column,
        end_column=end_column,
        confidence="high",
    )


def _check_class(relpath: str, cls: ClassInfo) -> list[Violation]:
    """Flag an undocumented class and any of its undocumented methods."""
    violations: list[Violation] = []
    if not cls.has_doc:
        violations.append(
            _missing_doc_violation(relpath, cls.start_line, cls.name, cls.kind, cls.start_column, cls.end_column)
        )
    for method in cls.methods:
        if not method.has_doc:
            violations.append(
                _missing_doc_violation(
                    relpath, method.start_line, method.qualified_name, "method", method.start_column, method.end_column
                )
            )
    return violations


def _check_function(relpath: str, func: FunctionInfo) -> list[Violation]:
    """Flag an undocumented top-level function."""
    if func.has_doc:
        return []
    return [
        _missing_doc_violation(relpath, func.start_line, func.name, "function", func.start_column, func.end_column)
    ]


def check(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Flag classes, methods and top-level functions with no doc comment."""
    if not config["rules"].get("require_docstrings"):
        return []

    violations: list[Violation] = []
    for cls in analysis.classes:
        violations += _check_class(analysis.relpath, cls)
    for func in analysis.top_level_functions:
        violations += _check_function(analysis.relpath, func)
    return violations
