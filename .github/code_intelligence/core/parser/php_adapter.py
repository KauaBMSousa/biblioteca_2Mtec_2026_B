"""PHP language adapter, backed by `tree-sitter-php`.

Unlike the other three tree-sitter grammar packages, `tree_sitter_php`
exposes `language_php()` rather than `language()` — confirmed against the
installed 0.24.1 wheel (`language_php_only` also exists but is not used
here since it excludes the surrounding HTML-interpolation grammar).

Ported from `tools/code_quality/code_quality/adapters/php_adapter.py`.
`trait_declaration` is relabeled from the old tool's generic `"class"` kind
to a precise `"trait"` kind (fixme §7's namespace/class/interface/trait/
enum/function/method vocabulary) — a pure relabeling, so it cannot change
`MULTIPLE_TYPE_DEFINITION`/`FILE_CLASS_MISMATCH` violation counts.
`__construct`/`__destruct` are PHP's naming convention for constructors/
destructors (no dedicated node type), handled via `constructor_names`/
`destructor_names`.
"""

from pathlib import Path

from tree_sitter import Language
from tree_sitter_php import language_php

from code_intelligence.core.parser._treesitter_common import (
    LanguageGrammar,
    analyze_with_grammar,
    blocks_containing_with_grammar,
    outline_with_grammar,
)
from code_intelligence.core.parser.base import LanguageAdapter
from code_intelligence.core.parser.ir import FileAnalysis

_GRAMMAR = LanguageGrammar(
    language_name="php",
    ts_language=Language(language_php()),
    class_node_types={
        "class_declaration": "class",
        "interface_declaration": "interface",
        "trait_declaration": "trait",
    },
    function_node_types={"function_definition", "method_declaration"},
    decision_node_types={
        "if_statement",
        "for_statement",
        "foreach_statement",
        "while_statement",
        "do_statement",
        "case_statement",
        "catch_clause",
        "conditional_expression",
    },
    nesting_node_types={
        "if_statement",
        "for_statement",
        "foreach_statement",
        "while_statement",
        "do_statement",
        "switch_statement",
        "try_statement",
        "compound_statement",
    },
    comment_node_types={"comment"},
    doc_prefixes=("/**",),
    import_node_types={"namespace_use_declaration"},
    line_comment_prefixes=("//", "#"),
    call_node_types={"function_call_expression", "member_call_expression", "scoped_call_expression"},
    instantiation_node_types={"object_creation_expression"},
    constructor_names=("__construct",),
    destructor_names=("__destruct",),
)


class PhpAdapter(LanguageAdapter):
    """Adapter for `.php` source using `tree-sitter-php`."""

    language = "php"
    extensions = (".php",)

    def analyze(self, path: Path, source: str) -> FileAnalysis:
        """Parse PHP source into the common IR via the shared tree-sitter engine."""
        return analyze_with_grammar(path, source, _GRAMMAR)

    def blocks_containing(self, source: str, line: int) -> list[dict] | None:
        """Exact line spans of every block containing `line` (see base)."""
        return blocks_containing_with_grammar(source, line, _GRAMMAR)

    def outline(self, source: str, start_line: int, end_line: int, max_depth: int = 2) -> list[dict] | None:
        """Control-flow outline of a line span (see base)."""
        return outline_with_grammar(source, start_line, end_line, _GRAMMAR, max_depth)
