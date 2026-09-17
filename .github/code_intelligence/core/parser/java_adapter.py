"""Java language adapter, backed by `tree-sitter-java`.

Ported from `tools/code_quality/code_quality/adapters/java_adapter.py`.
`record_declaration`/`enum_declaration` are relabeled from the old tool's
generic `"class"` kind to precise `"record"`/`"enum"` kinds (fixme §7's
package/class/interface/enum/record/method/constructor vocabulary) — a pure
relabeling of nodes the extractor already captured, so it cannot change
`MULTIPLE_TYPE_DEFINITION`/`FILE_CLASS_MISMATCH` violation counts (both
rules count `len(analysis.classes)`/type names, not the `kind` string).
`constructor_declaration` is its own dedicated node type in this grammar,
so it's labeled via `function_kind_map` directly rather than by name
convention.
"""

from pathlib import Path

from tree_sitter import Language
from tree_sitter_java import language as java_language

from code_intelligence.core.parser._treesitter_common import (
    LanguageGrammar,
    analyze_with_grammar,
    blocks_containing_with_grammar,
    outline_with_grammar,
)
from code_intelligence.core.parser.base import LanguageAdapter
from code_intelligence.core.parser.ir import FileAnalysis

_GRAMMAR = LanguageGrammar(
    language_name="java",
    ts_language=Language(java_language()),
    class_node_types={
        "class_declaration": "class",
        "interface_declaration": "interface",
        "record_declaration": "record",
        "enum_declaration": "enum",
    },
    function_node_types={"method_declaration", "constructor_declaration"},
    decision_node_types={
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "switch_label",
        "catch_clause",
        "ternary_expression",
    },
    nesting_node_types={
        "if_statement",
        "for_statement",
        "enhanced_for_statement",
        "while_statement",
        "do_statement",
        "switch_expression",
        "try_statement",
        "block",
    },
    comment_node_types={"line_comment", "block_comment"},
    doc_prefixes=("/**",),
    import_node_types={"import_declaration"},
    call_node_types={"method_invocation"},
    instantiation_node_types={"object_creation_expression"},
    function_kind_map={"constructor_declaration": "constructor", "method_declaration": "method"},
)


class JavaAdapter(LanguageAdapter):
    """Adapter for `.java` source using `tree-sitter-java`."""

    language = "java"
    extensions = (".java",)

    def analyze(self, path: Path, source: str) -> FileAnalysis:
        """Parse Java source into the common IR via the shared tree-sitter engine."""
        return analyze_with_grammar(path, source, _GRAMMAR)

    def blocks_containing(self, source: str, line: int) -> list[dict] | None:
        """Exact line spans of every block containing `line` (see base)."""
        return blocks_containing_with_grammar(source, line, _GRAMMAR)

    def outline(self, source: str, start_line: int, end_line: int, max_depth: int = 2) -> list[dict] | None:
        """Control-flow outline of a line span (see base)."""
        return outline_with_grammar(source, start_line, end_line, _GRAMMAR, max_depth)
