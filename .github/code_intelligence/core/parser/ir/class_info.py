"""Defines :class:`ClassInfo`, the IR representation of a type definition."""

from dataclasses import dataclass, field

from code_intelligence.core.parser.ir.function_info import FunctionInfo


@dataclass(slots=True)
class ClassInfo:
    """Structural facts about one class, struct, interface, enum, namespace, etc.

    Attributes:
        name: The type's short name.
        kind: One of the per-language kinds from fixme §7, e.g. ``"class"``,
            ``"struct"``, ``"interface"``, ``"enum"``, ``"namespace"``,
            ``"trait"``, ``"record"``, ``"template"``, ``"concept"``,
            ``"module"``.
        start_line: 1-based line of the type's first token.
        end_line: 1-based line of the type's last token.
        has_doc: Whether a documentation comment/docstring immediately
            precedes (or, for Python, opens) the type definition.
        methods: The type's member functions, in declaration order.
        start_column: 1-based column of the type's first token.
        end_column: 1-based column of the type's last token.
        symbol_id: Stable, content-derived identifier computed by
            ``code_intelligence.core.symbols``. Empty string until populated.
        namespace: Enclosing namespace/module path, when known.
        kind_confidence: ``"high"``/``"medium"``/``"low"`` — how confidently
            the adapter identified this construct's *kind*. A grammar that
            can't cleanly expose a construct (e.g. C++ concepts) reports
            ``"medium"`` rather than silently being skipped or mislabeled.
    """

    name: str
    kind: str
    start_line: int
    end_line: int
    has_doc: bool
    methods: list[FunctionInfo] = field(default_factory=list)
    start_column: int = 1
    end_column: int = 1
    symbol_id: str = ""
    namespace: str | None = None
    kind_confidence: str = "high"
