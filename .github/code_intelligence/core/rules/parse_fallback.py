"""Parse-fallback rule: PARSE_FALLBACK.

Whenever the regex/brace-count fallback was used instead of the primary
AST/tree-sitter parser, tell the agent so, at WARNING severity (a downgrade
in analysis fidelity, not a code-quality defect by itself).

Ported unchanged (besides import paths) from
`tools/code_quality/code_quality/rules/parse_fallback.py`.
"""

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.parser.ir import FileAnalysis


def check(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Flag a file that was analyzed via the regex/brace-count fallback path."""
    if not analysis.parse_fallback_used:
        return []
    if analysis.is_embedded_listing:
        # Not a parse failure: the file is a document listing that never was
        # source in this language. `embedded_listing` reports that instead,
        # so the real fallbacks -- files that should parse and do not -- are
        # not diluted by files that never could.
        return []

    return [
        Violation(
            code="PARSE_FALLBACK",
            severity=Severity.WARNING,
            file=analysis.relpath,
            line=1,
            end_line=1,
            message=(
                f"'{analysis.relpath}' could not be fully parsed; structural "
                "findings for this file are based on a LOC-only fallback "
                "(low parse_confidence)"
            ),
            detail={"parse_error": analysis.parse_error},
            symbol_id=None,
            column=1,
            end_column=1,
            confidence="high",
        )
    ]
