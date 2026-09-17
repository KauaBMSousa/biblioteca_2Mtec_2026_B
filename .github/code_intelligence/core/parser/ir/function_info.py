"""Defines :class:`FunctionInfo`, the IR representation of a function or method."""

from dataclasses import dataclass, field

from code_intelligence.core.parser.ir.identifier_ref import IdentifierRef

#: A single token in a function's flattened token stream, used by the
#: duplication detector. ``kind`` is one of ``IDENT``, ``LITERAL``,
#: ``KEYWORD``, ``OP``.
Token = tuple[str, str]


@dataclass(slots=True)
class FunctionInfo:
    """Structural facts about one function or method, language-agnostic.

    Attributes:
        name: The function or method's short name.
        qualified_name: Dotted/scoped name including the owning class, when
            applicable (e.g. ``"Widget.render"``).
        start_line: 1-based line of the function's first token.
        end_line: 1-based line of the function's last token.
        physical_lines: ``end_line - start_line + 1``.
        has_doc: Whether a documentation comment/docstring was found
            immediately preceding (or, for Python, inside) the function.
        nesting_depth: Maximum block-nesting depth reached inside the body.
        cyclomatic_complexity: McCabe-style cyclomatic complexity.
        identifiers: All identifiers declared within the function body.
        token_stream: Flattened, normalized token sequence used for
            cross-file duplication detection.
        is_method: True when this function is a class/struct/interface
            member rather than a free function.
        owner_class: Name of the owning class when ``is_method`` is True.
        start_column: 1-based column of the function's first token.
        end_column: 1-based column of the function's last token.
        body_start_line: 1-based first line of the function's body, when it
            can be isolated from the declaration (falls back to
            ``start_line`` otherwise).
        body_end_line: 1-based last line of the function's body (falls back
            to ``end_line`` otherwise).
        symbol_id: Stable, content-derived identifier computed by
            ``code_intelligence.core.symbols``. Empty string until populated.
        namespace: Enclosing namespace/module path, when known.
        calls: symbol_ids this function is believed to call (project-wide
            name-matching heuristic — see ``code_intelligence.core.dependencies``).
        called_by: Inverse of ``calls``, built in the same pass.
        references: symbol_ids referenced but not confidently identified as
            calls (e.g. ambiguous multi-candidate name matches).
        call_graph_confidence: ``"medium"`` (same-file/unique match) or
            ``"low"`` (cross-file or ambiguous match) — never ``"high"``,
            since this is always a name-matching heuristic.
        kind_confidence: ``"high"``/``"medium"``/``"low"`` — how confidently
            the adapter identified this construct's *kind* (e.g. a
            tree-sitter grammar that can't cleanly distinguish a C++
            template from a plain function reports ``"medium"``). Every
            structural fact can declare this, not just call-graph edges.
    """

    name: str
    qualified_name: str
    start_line: int
    end_line: int
    physical_lines: int
    has_doc: bool
    nesting_depth: int
    cyclomatic_complexity: int
    identifiers: list[IdentifierRef] = field(default_factory=list)
    token_stream: list[Token] = field(default_factory=list)
    is_method: bool = False
    owner_class: str | None = None
    start_column: int = 1
    end_column: int = 1
    body_start_line: int = 0
    body_end_line: int = 0
    symbol_id: str = ""
    namespace: str | None = None
    calls: list[str] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    call_graph_confidence: str = "medium"
    kind: str = "function"
    kind_confidence: str = "high"
