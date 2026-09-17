"""Embedded-listing rule: NOT_SOURCE_FILE.

Some files carry a source extension without being source: a `.py` whose
content is

    \\begin{lstlisting}[language=Python, caption={...}]
    class Neuron:
        ...
    \\end{lstlisting}

is a LaTeX fragment a paper ``\\input``s. It has never parsed as Python and
never will, so the parser falls back and the file collects a
``PARSE_FALLBACK`` warning it can never clear — noise that also dilutes the
real parse failures, which are files that *should* parse and do not.

This rule says the true thing instead: the file is not source. It is still
reported (silence would hide a genuinely mis-extensioned file from someone
who wants to know), just as its own finding, at WARNING, and
``parse_fallback`` stays quiet for these files so the two never
double-report the same fact.
"""

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.parser.ir import FileAnalysis


def check(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Flag a file whose content is a document listing, not source."""
    if not analysis.is_embedded_listing:
        return []

    return [
        Violation(
            code="NOT_SOURCE_FILE",
            severity=Severity.WARNING,
            file=analysis.relpath,
            line=1,
            end_line=1,
            message=(
                f"'{analysis.relpath}' has a source extension but its content is a "
                "document listing (\\begin{lstlisting}/minted/verbatim), so it is "
                "not parsed as code; no structural finding for it means anything"
            ),
            detail={"language": analysis.language},
            symbol_id=None,
            column=1,
            end_column=1,
            confidence="high",
        )
    ]
