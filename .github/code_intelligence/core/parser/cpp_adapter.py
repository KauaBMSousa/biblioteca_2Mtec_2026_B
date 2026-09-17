"""C++ language adapter, backed by `tree-sitter-cpp`.

Extensions per the plan: `.hpp .hxx .hh .cpp .cc .cxx` from fixme.md §6,
plus `.h` (omitted from the spec's literal list but essential for real C++
codebases). Ported from `tools/code_quality/code_quality/adapters/cpp_adapter.py`.
"""

from pathlib import Path

from tree_sitter import Language, Node
from tree_sitter_cpp import language as cpp_language

from code_intelligence.core.parser._treesitter_common import (
    LanguageGrammar,
    analyze_with_grammar,
    blocks_containing_with_grammar,
    node_text_or_none,
    outline_with_grammar,
)
from code_intelligence.core.parser.base import LanguageAdapter
from code_intelligence.core.parser.ir import FileAnalysis

#: Safety cap on declarator-chain descent depth (pointer/reference/array
#: wrappers are never nested this deep in practice).
_MAX_DECLARATOR_DEPTH = 10


def _find_function_name(node: Node) -> str | None:
    """Descend a C++ declarator chain (pointer/reference/array wrappers) to the name.

    `function_definition.declarator` may be wrapped in `pointer_declarator`,
    `reference_declarator`, etc. before reaching the actual
    `function_declarator`, whose own `declarator` field holds the name.
    """
    declarator = node.child_by_field_name("declarator")
    depth = 0
    while declarator is not None and declarator.type != "function_declarator" and depth < _MAX_DECLARATOR_DEPTH:
        declarator = declarator.child_by_field_name("declarator")
        depth += 1
    if declarator is None or declarator.type != "function_declarator":
        return None
    return node_text_or_none(declarator.child_by_field_name("declarator"))


_GRAMMAR = LanguageGrammar(
    language_name="cpp",
    ts_language=Language(cpp_language()),
    class_node_types={"class_specifier": "class", "struct_specifier": "struct"},
    function_node_types={"function_definition"},
    decision_node_types={
        "if_statement",
        "for_statement",
        "for_range_loop",
        "while_statement",
        "do_statement",
        "case_statement",
        "catch_clause",
        "conditional_expression",
    },
    nesting_node_types={
        "if_statement",
        "for_statement",
        "for_range_loop",
        "while_statement",
        "do_statement",
        "switch_statement",
        "try_statement",
        "compound_statement",
    },
    comment_node_types={"comment"},
    doc_prefixes=("/**", "///", "/*!"),
    import_node_types={"preproc_include"},
    find_function_name=_find_function_name,
    call_node_types={"call_expression"},
    instantiation_node_types={"new_expression"},
    # fixme §7: C++ constructor/destructor are the same `function_definition`
    # node type as any other method — distinguished by name convention
    # (`~Name` for a destructor, `name == owner_class` for a constructor),
    # not by a dedicated node type the way Java's grammar exposes one.
    destructor_prefixes=("~",),
)


class CppAdapter(LanguageAdapter):
    """Adapter for C/C++ source using `tree-sitter-cpp`."""

    language = "cpp"
    extensions = (".hpp", ".hxx", ".hh", ".h", ".cpp", ".cc", ".cxx")

    def analyze(self, path: Path, source: str) -> FileAnalysis:
        """Parse C++ source into the common IR via the shared tree-sitter engine."""
        return analyze_with_grammar(path, source, _GRAMMAR)

    def blocks_containing(self, source: str, line: int) -> list[dict] | None:
        """Exact line spans of every block containing `line` (see base)."""
        return blocks_containing_with_grammar(source, line, _GRAMMAR)

    def outline(self, source: str, start_line: int, end_line: int, max_depth: int = 2) -> list[dict] | None:
        """Control-flow outline of a line span (see base)."""
        return outline_with_grammar(source, start_line, end_line, _GRAMMAR, max_depth)
