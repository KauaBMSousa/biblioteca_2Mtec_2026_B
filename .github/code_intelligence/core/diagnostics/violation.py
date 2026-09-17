"""Defines :class:`Violation` and the :class:`Severity` ordering.

Ported from `tools/code_quality/code_quality/ir/violation.py`, extended with
a first-class `confidence` field (fixme §11: "toda informação estrutural
deve poder declarar" high/medium/low confidence — not just call-graph
edges, as it was in the original tool).
"""

from dataclasses import dataclass
from enum import IntEnum


class Severity(IntEnum):
    """Ordered violation severity, from least to most serious.

    The integer ordering is load-bearing: report generation and exit-code
    mapping both rely on ``max()`` over a collection of severities picking
    the most serious one.
    """

    OK = 0
    WARNING = 1
    HIGH = 2
    CRITICAL = 3
    VERY_CRITICAL = 4


@dataclass(slots=True)
class Violation:
    """One reportable finding produced by a rule module.

    Attributes:
        code: A stable machine-readable violation code, e.g.
            ``"NAMING_VIOLATION"``, ``"MISSING_DOCSTRING"``,
            ``"FILE_CLASS_MISMATCH"``, ``"MULTIPLE_TYPE_DEFINITION"``,
            ``"PROCEDURAL_MONOLITH"``, ``"PARSE_FALLBACK"``,
            ``"DUPLICATE_BLOCK"``, ``"LOC_WARNING"``, ``"LOC_HIGH"``,
            ``"LOC_CRITICAL"``, ``"LOC_VERY_CRITICAL"``,
            ``"FUNCTION_LENGTH_WARNING"``, ``"FUNCTION_LENGTH_HIGH"``,
            ``"FUNCTION_LENGTH_CRITICAL"``.
        severity: The violation's :class:`Severity`.
        file: Repo-relative path of the offending file.
        line: 1-based start line of the offending span.
        end_line: 1-based end line of the offending span (equals ``line``
            for single-line findings).
        message: Short, human-readable summary.
        detail: Optional machine-readable extra context (counts, thresholds,
            related locations, etc.).
        symbol_id: The symbol_id of the enclosing function/class, when the
            violation is scoped to one symbol; None for file-level findings.
        column: 1-based start column of the offending span, when known.
        end_column: 1-based end column of the offending span, when known.
        confidence: ``"high"``/``"medium"``/``"low"`` — how confidently this
            finding was derived. Deterministic threshold rules (LOC,
            function length, naming, missing docstring, single-type-per-file)
            are ``"high"``. Heuristic rules (procedural monolith cohesion,
            cross-file duplication, parse-fallback-derived findings) are
            ``"medium"``. Never presented as certainty when it isn't one.
    """

    code: str
    severity: Severity
    file: str
    line: int
    end_line: int
    message: str
    detail: dict | None = None
    symbol_id: str | None = None
    column: int | None = None
    end_column: int | None = None
    confidence: str = "high"
