"""Defines :class:`SymbolRecord`, a flattened, agent-navigable view of one symbol.

Ported from `tools/code_quality/code_quality/ir/symbol_record.py`. This is
the shape persisted into the `symbols` table by `core/index/indexer.py` and
returned by `Workspace.get_symbol()`.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class SymbolRecord:
    """One flattened, structurally-addressable symbol.

    Attributes:
        kind: Per-language kind — see fixme §7's explicit lists, e.g. for
            C++: ``namespace``/``class``/``struct``/``enum``/``function``/
            ``method``/``constructor``/``destructor``/``template``/
            ``concept``; Java: ``package``/``class``/``interface``/``enum``/
            ``record``/``method``/``constructor``; PHP: ``namespace``/
            ``class``/``interface``/``trait``/``enum``/``function``/
            ``method``; JavaScript: ``module``/``class``/``function``/
            ``method``/``arrow_function``/``export``/``import``.
        name: The symbol's short name.
        qualified_name: Dotted/scoped name including the owning class, when
            applicable.
        symbol_id: Stable, content-derived identifier
            (``{language}:{relpath}:{kind}:{qualified_name}[/{param_count}][#{hash}]``).
        file: Repo-relative path of the defining file.
        language: One of ``"python"``, ``"cpp"``, ``"java"``, ``"php"``,
            ``"javascript"``.
        start_line: 1-based line of the symbol's first token.
        end_line: 1-based line of the symbol's last token.
        start_column: 1-based column of the symbol's first token.
        end_column: 1-based column of the symbol's last token.
        body_start_line: 1-based first line of the symbol's body span.
        body_end_line: 1-based last line of the symbol's body span.
        loc: Physical line count of the symbol's span.
        cyclomatic_complexity: McCabe-style complexity; ``None`` for
            classes/types (only meaningful for functions/methods).
        has_doc: Whether a documentation comment was found.
        namespace: Enclosing namespace/module path, when known.
        calls: symbol_ids this symbol is believed to call.
        called_by: Inverse of ``calls``.
        references: symbol_ids referenced but not confidently a call.
        call_graph_confidence: ``"medium"`` or ``"low"`` — never ``"high"``.
        content_hash: ``sha256:...`` hash of the symbol's exact source span,
            independently reproducible via ``sha256sum`` on that byte range.
        violations: Violation codes attached to this symbol.
        confidence: ``"high"``/``"medium"``/``"low"`` — how confidently this
            symbol's *kind* was identified (fixme §11: every structural
            fact should be able to declare confidence, not just call-graph
            edges). A grammar construct that can't be cleanly classified
            (e.g. a C++ concept) is still recorded, tagged ``"medium"``,
            rather than silently dropped.
    """

    kind: str
    name: str
    qualified_name: str
    symbol_id: str
    file: str
    language: str
    start_line: int
    end_line: int
    start_column: int
    end_column: int
    body_start_line: int
    body_end_line: int
    loc: int
    cyclomatic_complexity: int | None
    has_doc: bool
    namespace: str | None = None
    calls: list[str] = field(default_factory=list)
    called_by: list[str] = field(default_factory=list)
    references: list[str] = field(default_factory=list)
    call_graph_confidence: str = "medium"
    content_hash: str = ""
    violations: list[str] = field(default_factory=list)
    confidence: str = "high"
