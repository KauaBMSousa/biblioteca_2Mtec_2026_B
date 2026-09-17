"""JavaScript language adapter, backed by `tree-sitter-javascript`.

TypeScript (`.ts`/`.tsx`) is explicitly optional per fixme.md §6 and no
grammar package is installed for it — deferred, not built in this pass.

Ported from `tools/code_quality/code_quality/adapters/javascript_adapter.py`.
JS classes name their constructor method literally `"constructor"` (no
separate node type, no `~`-prefix convention) — handled via
`constructor_names`. Arrow functions/`export`/`import` are deliberately
*not* extracted as new first-class symbols in Phase A (see
`_treesitter_common.py`'s module docstring for why: doing so would change
`top_level_functions` cardinality and risk breaking the parity proof
against the pre-existing `tools/code_quality` baseline).
"""

from pathlib import Path

from tree_sitter import Language
from tree_sitter_javascript import language as javascript_language

from code_intelligence.core.parser._treesitter_common import (
    LanguageGrammar,
    analyze_with_grammar,
    blocks_containing_with_grammar,
    outline_with_grammar,
)
from code_intelligence.core.parser.base import LanguageAdapter
from code_intelligence.core.parser.ir import FileAnalysis

_GRAMMAR = LanguageGrammar(
    language_name="javascript",
    ts_language=Language(javascript_language()),
    class_node_types={"class_declaration": "class"},
    function_node_types={"function_declaration", "method_definition"},
    decision_node_types={
        "if_statement",
        "for_statement",
        "for_in_statement",
        "while_statement",
        "do_statement",
        "switch_case",
        "catch_clause",
        "ternary_expression",
    },
    nesting_node_types={
        "if_statement",
        "for_statement",
        "for_in_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "try_statement",
        "statement_block",
    },
    comment_node_types={"comment"},
    doc_prefixes=("/**",),
    import_node_types={"import_statement"},
    call_node_types={"call_expression"},
    instantiation_node_types={"new_expression"},
    constructor_names=("constructor",),
)


class JavaScriptAdapter(LanguageAdapter):
    """Adapter for `.js`/`.jsx`/`.mjs`/`.cjs` source using `tree-sitter-javascript`."""

    language = "javascript"
    extensions = (".js", ".jsx", ".mjs", ".cjs")

    def analyze(self, path: Path, source: str) -> FileAnalysis:
        """Parse JavaScript source into the common IR via the shared tree-sitter engine."""
        return analyze_with_grammar(path, source, _GRAMMAR)

    def blocks_containing(self, source: str, line: int) -> list[dict] | None:
        """Exact line spans of every block containing `line` (see base)."""
        return blocks_containing_with_grammar(source, line, _GRAMMAR)

    def outline(self, source: str, start_line: int, end_line: int, max_depth: int = 2) -> list[dict] | None:
        """Control-flow outline of a line span (see base)."""
        return outline_with_grammar(source, start_line, end_line, _GRAMMAR, max_depth)
