"""Minimum-identifier-length rule (fixme.md §4.2): NAMING_VIOLATION.

The authoritative exception list is the configured `naming_exceptions`
(default: loop indices `i, j, k` plus a few conventional short names) —
this is checked by name, not by an adapter-supplied `is_exception` hint,
since only the Python adapter can compute that hint precisely; the four
tree-sitter adapters produce a best-effort approximation that must not be
treated as authoritative for suppressing real violations.

Ported unchanged (besides import paths) from
`tools/code_quality/code_quality/rules/naming.py`.
"""

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.parser.ir import FileAnalysis

#: Only *declaration* identifiers are subject to the naming-length rule.
#: `"function"`/`"class"` identifiers are call/instantiation *references*
#: (added for `core/dependencies`' name-matching index) — flagging a
#: reference to an already-declared name would double-count the same
#: violation already raised at the symbol's own declaration site.
_DECLARATION_KINDS = {"parameter", "variable", "loop_index"}


def _all_identifiers(analysis: FileAnalysis):
    """Yield every declaration identifier anywhere in one file, function-scoped."""
    for func in analysis.top_level_functions:
        yield from (ident for ident in func.identifiers if ident.kind in _DECLARATION_KINDS)
    for cls in analysis.classes:
        for method in cls.methods:
            yield from (ident for ident in method.identifiers if ident.kind in _DECLARATION_KINDS)


def check(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Flag identifiers shorter than `rules.naming_min_length`, honoring exceptions."""
    if not config["rules"].get("naming_min_length"):
        return []
    min_length = config["rules"]["naming_min_length"]
    exceptions = set(config.get("naming_exceptions", []))

    violations: list[Violation] = []
    seen: set[tuple[str, int]] = set()
    for ident in _all_identifiers(analysis):
        if len(ident.name) >= min_length:
            continue
        if ident.name in exceptions:
            continue
        key = (ident.name, ident.line)
        if key in seen:
            continue
        seen.add(key)
        violations.append(
            Violation(
                code="NAMING_VIOLATION",
                severity=Severity.WARNING,
                file=analysis.relpath,
                line=ident.line,
                end_line=ident.line,
                message=(
                    f"Identifier '{ident.name}' ({ident.kind}) is shorter than the "
                    f"configured minimum of {min_length} characters"
                ),
                detail={"name": ident.name, "kind": ident.kind, "min_length": min_length},
                column=ident.column,
                end_column=ident.column + len(ident.name),
                confidence="high",
            )
        )
    return violations
