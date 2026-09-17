"""Shared tree-sitter driven analysis engine for the C++/Java/PHP/JavaScript adapters.

Each of those four grammars differs only in node-type *names*; the actual
analysis logic (LOC counting, doc-comment lookback, nesting depth,
cyclomatic complexity, token-stream flattening, identifier classification)
is identical. This module implements that logic once, parameterized by a
:class:`LanguageGrammar` node-type table, so the four adapter modules stay
thin (grammar table + regex fallback + entrypoint) instead of duplicating
~200 lines of tree-walking each.

Ported from `tools/code_quality/code_quality/adapters/_treesitter_common.py`,
extended with `function_kind_map` (a per-node-type kind label, e.g. Java's
`constructor_declaration` -> `"constructor"`) plus name-convention kind
inference (C++ `~Name` destructors, PHP `__construct`/`__destruct`, a
same-name-as-owner-class constructor) so fixme §7's per-language kind
vocabulary is precisely labeled on every construct the extraction surface
already captured — deliberately *not* extended to capture brand-new node
types (namespaces, templates, concepts, JS arrow functions/export/import)
in Phase A, since that would change `classes`/`top_level_functions`
cardinality and risk breaking the parity proof against the pre-existing
`tools/code_quality` baseline. See the Phase A final report for the full
rationale.
"""

import hashlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from tree_sitter import Language, Node, Parser

from code_intelligence.core.parser.ir import (
    ClassInfo,
    FileAnalysis,
    FunctionInfo,
    IdentifierRef,
    ImportInfo,
    Token,
)


@dataclass(frozen=True, slots=True)
class LanguageGrammar:
    """Per-language node-type table driving the shared tree-sitter engine.

    Attributes:
        language_name: e.g. "cpp", "java", "php", "javascript".
        ts_language: The `tree_sitter.Language` grammar instance.
        class_node_types: Maps node.type -> IR ``kind`` ("class"/"struct"/
            "interface"/"enum"/"record"/"trait"/...) for type-definition
            nodes.
        function_node_types: Node types that define a function or method.
        decision_node_types: Node types counted once each toward cyclomatic
            complexity (base complexity is always 1).
        nesting_node_types: Node types that open a nested block for the
            purpose of computing nesting depth.
        comment_node_types: Node types representing a comment.
        doc_prefixes: Comment text prefixes that mark a comment as a
            documentation comment (e.g. ``("/**",)``).
        import_node_types: Node types representing an import/include/use
            statement.
        find_function_name: Extracts a function/method node's short name;
            None if not extractable (skips the function).
        line_comment_prefixes: Prefixes for regex-fallback comment detection.
        block_comment_delims: (start, end) markers for regex-fallback block
            comments, or None if the language has none.
        call_node_types: Node types representing a function/method call
            (used only to build call/instantiation-like `IdentifierRef`s
            for `core/dependencies`' name-matching index — never fed into
            naming-length checks).
        instantiation_node_types: Node types representing an object/class
            instantiation (e.g. `new Widget()`); tagged with `kind="class"`
            instead of `kind="function"`.
        function_kind_map: Maps node.type -> a precise IR kind label (e.g.
            Java's `constructor_declaration` -> `"constructor"`), applied
            before the generic name-convention inference below.
        destructor_prefixes: Name prefixes that mark a method as a
            destructor (e.g. C++'s `~`).
        destructor_names: Exact method names that mark a destructor (e.g.
            PHP's `__destruct`).
        constructor_names: Exact method names that mark a constructor in
            addition to the generic "name equals owner class name" rule
            (e.g. PHP's `__construct`).
    """

    language_name: str
    ts_language: Language
    class_node_types: dict[str, str]
    function_node_types: set[str]
    decision_node_types: set[str]
    nesting_node_types: set[str]
    comment_node_types: set[str]
    doc_prefixes: tuple[str, ...]
    import_node_types: set[str] = field(default_factory=set)
    find_function_name: Callable[[Node], str | None] | None = None
    line_comment_prefixes: tuple[str, ...] = ("//",)
    block_comment_delims: tuple[str, str] | None = ("/*", "*/")
    call_node_types: set[str] = field(default_factory=set)
    instantiation_node_types: set[str] = field(default_factory=set)
    function_kind_map: dict[str, str] = field(default_factory=dict)
    destructor_prefixes: tuple[str, ...] = ()
    destructor_names: tuple[str, ...] = ()
    constructor_names: tuple[str, ...] = ()


def node_text_or_none(node: Node | None) -> str | None:
    """Decode a node's UTF-8 text, or return None when the node itself is None.

    Shared by every adapter's function/class name extraction so the
    "look up a field, bail on None, decode the text" idiom lives in exactly
    one place instead of being repeated per language.
    """
    if node is None:
        return None
    return node.text.decode("utf-8", errors="replace")


def _default_find_name(node: Node) -> str | None:
    """Default name extraction via the uniform 'name' field."""
    return node_text_or_none(node.child_by_field_name("name"))


def _decode(node: Node) -> str:
    """Decode a node's source text as UTF-8, replacing invalid bytes."""
    return node.text.decode("utf-8", errors="replace")


def _preceding_doc_comment(node: Node, grammar: LanguageGrammar) -> bool:
    """Return True when a documentation comment immediately precedes `node`.

    Looks at the previous named sibling; a blank-line gap is tolerated up to
    one line, matching common Javadoc/JSDoc/PHPDoc/`/** */` placement.
    Also handles the case where the function is inside an `export_statement`
    by checking the export node's previous sibling.
    """
    # First check the direct previous sibling
    prev = node.prev_sibling
    if prev is not None and prev.type in grammar.comment_node_types:
        text = _decode(prev)
        if any(text.startswith(prefix) for prefix in grammar.doc_prefixes):
            return True

    # If the node is inside an export_statement, check the export's siblings
    parent = node.parent
    if parent is not None and parent.type == "export_statement":
        prev = parent.prev_sibling
        if prev is not None and prev.type in grammar.comment_node_types:
            text = _decode(prev)
            if any(text.startswith(prefix) for prefix in grammar.doc_prefixes):
                return True

    return False


def _is_named_identifier_text(text: str) -> bool:
    """Return True when `text` looks like a plain identifier token."""
    return text.isidentifier()


def _build_token_stream(node: Node, grammar: LanguageGrammar) -> list[Token]:
    """Flatten all leaf tokens under `node` into a normalized token stream.

    Classification is grammar-agnostic: unnamed (anonymous) leaves are
    KEYWORD when their text is identifier-shaped, else OP; named leaves are
    IDENT when identifier-shaped, else LITERAL. This works uniformly across
    all four tree-sitter grammars without a per-language identifier table.
    """
    stream: list[Token] = []

    def walk(current: Node) -> None:
        if current.type in grammar.comment_node_types:
            return
        if current.child_count == 0:
            text = _decode(current)
            if not text.strip():
                return
            if current.is_named:
                kind = "IDENT" if _is_named_identifier_text(text) else "LITERAL"
            else:
                kind = "KEYWORD" if _is_named_identifier_text(text) else "OP"
            stream.append((kind, text))
            return
        for child in current.children:
            walk(child)

    walk(node)
    return stream


def _max_nesting_depth(node: Node, grammar: LanguageGrammar, function_node_types: set[str]) -> int:
    """Compute the deepest nesting-node depth reached under `node`."""
    best = 0

    def walk(current: Node, depth: int) -> None:
        nonlocal best
        best = max(best, depth)
        for child in current.children:
            if child.type in function_node_types or child.type in grammar.class_node_types:
                continue  # nested scopes get their own accounting
            next_depth = depth + 1 if child.type in grammar.nesting_node_types else depth
            walk(child, next_depth)

    walk(node, 0)
    return best


def _cyclomatic_complexity(node: Node, grammar: LanguageGrammar, function_node_types: set[str]) -> int:
    """McCabe-style complexity: 1 + count of decision nodes under `node`."""
    complexity = 1

    def walk(current: Node) -> None:
        nonlocal complexity
        for child in current.children:
            if child.type in function_node_types or child.type in grammar.class_node_types:
                continue
            if child.type in grammar.decision_node_types:
                complexity += 1
            walk(child)

    walk(node)
    return complexity


#: Wrapper nodes that sit between a declared name and the node that declares
#: it: C++ writes `int& x` as reference_declarator(identifier), PHP writes
#: every `$x` as variable_name(name), and JS groups parameters in a
#: formal_parameters list. Walking through them is what lets one rule serve
#: four grammars.
_DECLARATOR_WRAPPERS = {"variable_name", "formal_parameters", "parameters", "parameter_list"}

#: Parent node types for which a `name` field really means "this is being
#: declared". `name` is also the field of a *call* (`method_invocation[name]`
#: in Java, `member_call_expression[name]` in PHP), which is why the parent
#: type has to be checked and not just the field.
_DECLARING_PARENT_MARKERS = ("declarat", "parameter", "field_declaration", "property")


def _field_name_of(parent: Node, child: Node) -> str | None:
    """The grammar field linking `child` to `parent`, or None when unnamed."""
    for index in range(parent.child_count):
        if parent.child(index) == child:
            return parent.field_name_for_child(index)
    return None


#: Languages where a variable is introduced by its first assignment rather
#: than by a declaration statement. Everywhere else `total = total + 1` is a
#: RE-assignment, and counting it as a declaration reports the same variable
#: once per line that writes to it.
_ASSIGNMENT_DECLARES = {"php"}


def _is_declaration(leaf: Node, scope_root: Node, language_name: str = "") -> bool:
    """Whether this identifier leaf is being DECLARED here, or merely used.

    The distinction is the whole difference between a naming rule that says
    something and one that shouts. Without it, `nn` in
    `const nn::windowing::WindowSpec&` is reported as a badly named variable
    (it is a namespace), `at` in `result.at(i, j)` is reported as a badly
    named loop index (it is a method), and a variable called `n` is reported
    once per line that mentions it rather than once where it is introduced.
    On this project that was 16980 of 22008 warnings.

    The tree already answers it: the field name linking a leaf to its parent
    says what role the leaf plays. `declarator`/`name` on a declaring node
    introduces a name; `scope`, `type`, `field`, `object`, `argument` do not.
    """
    node = leaf
    parent = node.parent
    # A declared name is at most a few wrapper hops from its declarator; the
    # bound keeps a deeply nested expression from wandering up into the
    # enclosing declaration and calling itself declared.
    for _ in range(4):
        if parent is None or node == scope_root:
            return False
        field = _field_name_of(parent, node)
        parent_type = parent.type

        if field == "declarator":
            return True
        if field == "parameters":
            return True
        if field == "name" and any(mark in parent_type for mark in _DECLARING_PARENT_MARKERS):
            return True
        # PHP has no declaration statement: the first assignment introduces
        # the variable, so `$n = 0` is where `n` is named. In every other
        # language here an assignment writes to something already declared.
        if field == "left" and "assignment" in parent_type:
            return language_name in _ASSIGNMENT_DECLARES

        transparent = parent_type in _DECLARATOR_WRAPPERS or parent_type.endswith("_declarator")
        if not transparent:
            return False
        node, parent = parent, parent.parent
    return False


def _classify_identifier(leaf: Node, scope_root: Node, language_name: str = "") -> tuple[str, bool]:
    """Classify a leaf identifier by its role, walking ancestors up to `scope_root`.

    A leaf that is not being declared is a `"reference"` -- it is still
    collected (the procedural-monolith rule wants the whole vocabulary a
    function touches) but it is not a naming decision anyone made here, so
    the naming rule leaves it alone.

    Among declarations: an ancestor whose type mentions "parameter" makes it
    a parameter; an ancestor starting with a loop keyword makes it a loop
    index, exempted the way `i`/`j`/`k` conventionally are. The rest are
    plain variables.
    """
    if not _is_declaration(leaf, scope_root, language_name):
        return "reference", False

    ancestor = leaf.parent
    while ancestor is not None and ancestor != scope_root:
        node_type = ancestor.type
        if "parameter" in node_type:
            return "parameter", False
        if node_type.startswith(("for", "foreach", "while")):
            return "loop_index", True
        ancestor = ancestor.parent
    return "variable", False


#: Container node types holding a call's argument list — excluded when
#: hunting for the "callee expression" child of a call/instantiation node.
_ARG_CONTAINER_TYPES = {"argument_list", "arguments", "formal_parameters"}


def _rightmost_identifier_text(node: Node) -> str | None:
    """Find the rightmost identifier-shaped leaf under `node` (or `node` itself).

    Handles both a direct call (`helper(...)`, callee is a plain
    identifier leaf) and a member/field call (`obj.method(...)`, callee is
    a compound expression whose *last* identifier-shaped leaf is the
    actually-invoked name) with one generic, grammar-agnostic walk.
    """
    if node.child_count == 0:
        text = _decode(node)
        return text if _is_named_identifier_text(text) else None
    for child in reversed(node.children):
        if not child.is_named:
            continue
        found = _rightmost_identifier_text(child)
        if found:
            return found
    return None


def _call_reference_name(call_node: Node) -> str | None:
    """Extract the callee/instantiated-type name from a call/instantiation node."""
    callee_children = [
        child for child in call_node.children if child.is_named and child.type not in _ARG_CONTAINER_TYPES
    ]
    if not callee_children:
        return None
    return _rightmost_identifier_text(callee_children[-1])


def _collect_identifiers(func_node: Node, grammar: LanguageGrammar) -> list[IdentifierRef]:
    """Collect identifier-shaped leaves within `func_node`, excluding nested scopes."""
    identifiers: list[IdentifierRef] = []

    def walk(current: Node) -> None:
        is_nested_scope = current.type in grammar.function_node_types or current.type in grammar.class_node_types
        if current is not func_node and is_nested_scope:
            return  # nested scope owns its own identifiers
        if current.type in grammar.call_node_types or current.type in grammar.instantiation_node_types:
            name = _call_reference_name(current)
            if name:
                kind = "class" if current.type in grammar.instantiation_node_types else "function"
                identifiers.append(
                    IdentifierRef(
                        name=name, kind=kind, line=current.start_point[0] + 1, column=current.start_point[1] + 1
                    )
                )
            # Fall through (no return): still walk children so identifiers
            # used as call arguments are still captured.
        if current.child_count == 0 and current.is_named:
            text = _decode(current)
            if _is_named_identifier_text(text):
                kind, is_exception = _classify_identifier(current, func_node, grammar.language_name)
                identifiers.append(
                    IdentifierRef(
                        name=text,
                        kind=kind,
                        line=current.start_point[0] + 1,
                        is_exception=is_exception,
                        column=current.start_point[1] + 1,
                    )
                )
            return
        for child in current.children:
            walk(child)

    walk(func_node)
    return identifiers


def _infer_function_kind(node: Node, grammar: LanguageGrammar, name: str, owner_class: str | None) -> str:
    """Resolve a function/method's precise IR kind (fixme §7's per-language vocabulary).

    Checks, in order: an explicit `function_kind_map` entry for this node's
    type (the most reliable signal, e.g. Java's dedicated
    `constructor_declaration` node type); then destructor/constructor name
    conventions; falling back to the generic "method" (owner_class set) or
    "function" (free function) label. Every branch here is a precise,
    deterministic relabeling of a node the adapter was already extracting —
    it never changes *which* nodes get extracted, only how they're labeled,
    so it cannot change violation counts derived from list membership.
    """
    mapped = grammar.function_kind_map.get(node.type)
    if mapped is not None:
        return mapped
    if owner_class is None:
        return "function"
    if any(name.startswith(prefix) for prefix in grammar.destructor_prefixes):
        return "destructor"
    if name in grammar.destructor_names:
        return "destructor"
    if name in grammar.constructor_names or name == owner_class:
        return "constructor"
    return "method"


def _build_function_info(
    node: Node, grammar: LanguageGrammar, owner_class: str | None
) -> FunctionInfo | None:
    """Build a FunctionInfo for one function/method node, or None if unnamed."""
    find_name = grammar.find_function_name or _default_find_name
    name = find_name(node)
    if not name:
        return None
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1
    start_column = node.start_point[1] + 1
    end_column = node.end_point[1] + 1
    body = node.child_by_field_name("body") or node
    body_start_line = body.start_point[0] + 1
    body_end_line = body.end_point[0] + 1
    has_doc = _preceding_doc_comment(node, grammar)
    qualified_name = f"{owner_class}.{name}" if owner_class else name
    return FunctionInfo(
        name=name,
        qualified_name=qualified_name,
        start_line=start_line,
        end_line=end_line,
        physical_lines=end_line - start_line + 1,
        has_doc=has_doc,
        nesting_depth=_max_nesting_depth(body, grammar, grammar.function_node_types),
        cyclomatic_complexity=_cyclomatic_complexity(body, grammar, grammar.function_node_types),
        identifiers=_collect_identifiers(node, grammar),
        token_stream=_build_token_stream(node, grammar),
        is_method=owner_class is not None,
        owner_class=owner_class,
        start_column=start_column,
        end_column=end_column,
        body_start_line=body_start_line,
        body_end_line=body_end_line,
        kind=_infer_function_kind(node, grammar, name, owner_class),
    )


def _iter_functions_in_class_body(class_node: Node, grammar: LanguageGrammar) -> list[Node]:
    """Return direct-child function/method nodes within a class body."""
    body = class_node.child_by_field_name("body")
    if body is None:
        return []
    return [child for child in body.children if child.type in grammar.function_node_types]


def _build_class_info(node: Node, grammar: LanguageGrammar) -> ClassInfo | None:
    """Build a ClassInfo for one class/struct/interface/enum/record/trait node.

    Always uses the uniform 'name' field — even for C++, whose specialized
    `find_function_name` (declarator-chain descent) applies only to
    functions/methods, not to class/struct names.
    """
    name = _default_find_name(node)
    if not name:
        return None
    start_line = node.start_point[0] + 1
    end_line = node.end_point[0] + 1
    start_column = node.start_point[1] + 1
    end_column = node.end_point[1] + 1
    has_doc = _preceding_doc_comment(node, grammar)
    kind = grammar.class_node_types.get(node.type, "class")
    methods = [
        built
        for member in _iter_functions_in_class_body(node, grammar)
        if (built := _build_function_info(member, grammar, owner_class=name)) is not None
    ]
    return ClassInfo(
        name=name,
        kind=kind,
        start_line=start_line,
        end_line=end_line,
        has_doc=has_doc,
        methods=methods,
        start_column=start_column,
        end_column=end_column,
    )


def _walk_top_level(root: Node, grammar: LanguageGrammar) -> tuple[list[ClassInfo], list[FunctionInfo]]:
    """Walk the tree collecting all classes and all non-method (free) functions.

    Descends through non-scope "glue" nodes (namespaces, templates, export
    wrappers) but stops recursing into a class body once the class itself is
    recorded (its methods are collected via `_build_class_info`), and does
    not treat a function nested inside another function as a second
    top-level function.
    """
    classes: list[ClassInfo] = []
    functions: list[FunctionInfo] = []

    def walk(current: Node, inside_function: bool) -> None:
        for child in current.children:
            if child.type in grammar.class_node_types:
                class_info = _build_class_info(child, grammar)
                if class_info is not None:
                    classes.append(class_info)
                continue  # methods already captured; don't also treat as free functions
            if child.type in grammar.function_node_types:
                if not inside_function:
                    func_info = _build_function_info(child, grammar, owner_class=None)
                    if func_info is not None:
                        functions.append(func_info)
                walk(child, inside_function=True)
                continue
            walk(child, inside_function)

    walk(root, inside_function=False)
    return classes, functions


def _collect_imports(root: Node, grammar: LanguageGrammar) -> list[ImportInfo]:
    """Collect import/include/use statements matching the grammar's import node types."""
    imports: list[ImportInfo] = []
    if not grammar.import_node_types:
        return imports

    def walk(current: Node) -> None:
        if current.type in grammar.import_node_types:
            text = _decode(current).strip().splitlines()[0]
            imports.append(ImportInfo(module=text, line=current.start_point[0] + 1))
        for child in current.children:
            walk(child)

    walk(root)
    return imports


def _line_kinds_treesitter(root: Node, grammar: LanguageGrammar) -> tuple[set[int], set[int]]:
    """Classify physical lines as code/comment by scanning tree-sitter leaves."""
    code_lines: set[int] = set()
    comment_lines: set[int] = set()

    def walk(current: Node) -> None:
        if current.child_count == 0:
            text = _decode(current)
            if not text.strip():
                return
            span = range(current.start_point[0] + 1, current.end_point[0] + 2)
            if current.type in grammar.comment_node_types:
                comment_lines.update(span)
            else:
                code_lines.update(span)
            return
        for child in current.children:
            walk(child)

    walk(root)
    comment_lines -= code_lines
    return code_lines, comment_lines


def _regex_fallback_loc(source: str, grammar: LanguageGrammar) -> tuple[int, int, int, int]:
    """Regex/brace-count LOC fallback used when parsing fails or the tree has errors."""
    lines = source.splitlines()
    physical_lines = len(lines)
    comment_lines = 0
    blank_lines = 0
    in_block_comment = False
    block_start, block_end = grammar.block_comment_delims or (None, None)

    for line in lines:
        stripped = line.strip()
        if not stripped:
            blank_lines += 1
            continue
        if in_block_comment:
            comment_lines += 1
            if block_end and block_end in stripped:
                in_block_comment = False
            continue
        if any(stripped.startswith(prefix) for prefix in grammar.line_comment_prefixes):
            comment_lines += 1
            continue
        if block_start and stripped.startswith(block_start):
            comment_lines += 1
            if block_end and block_end not in stripped[len(block_start):]:
                in_block_comment = True
            continue

    logical_code_lines = physical_lines - comment_lines - blank_lines
    return physical_lines, max(logical_code_lines, 0), comment_lines, blank_lines


def _first_error_location(root: Node, source: str) -> tuple[int, str] | None:
    """Line (1-based) and text of the first ERROR/missing node, if any.

    Depth-first, stopping at the first offending node rather than
    descending into it: its children are the debris of the failure, not
    the failure.
    """
    stack = [root]
    while stack:
        node = stack.pop(0)
        if node.type == "ERROR" or node.is_missing:
            line_number = node.start_point[0] + 1
            lines = source.splitlines()
            text = lines[line_number - 1].strip() if line_number - 1 < len(lines) else ""
            return line_number, text[:160]
        stack = list(node.children) + stack
    return None


def _fallback_file_analysis(
    path: Path,
    source: str,
    grammar: LanguageGrammar,
    file_hash: str,
    error_location: tuple[int, str] | None = None,
) -> FileAnalysis:
    """Build a LOC-only FileAnalysis via the regex fallback (parse error path).

    The message carries WHERE the parse failed, because "this file could
    not be parsed" is not actionable and the two causes need opposite
    responses. Both, from one real C++ codebase:

        Tensor.hpp:1   #/**                     <- a stray '#'. Their typo,
                                                   one character to fix.
        Trainer.hpp:162  ... = {}) -> std::vector<EpochResult>
                                                <- valid C++20 the grammar
                                                   cannot parse. Nothing to fix.

    Without the location the two are the same warning, so all 16 of them
    get ignored together -- including the two that were real.
    """
    physical_lines, logical_code_lines, comment_lines, blank_lines = _regex_fallback_loc(source, grammar)
    if error_location is None:
        detail = "tree-sitter reported a syntax error in this file"
    else:
        line_number, text = error_location
        detail = f"tree-sitter could not parse this file, starting at line {line_number}: {text!r}"
    return FileAnalysis(
        path=str(path),
        relpath=path.name,
        language=grammar.language_name,
        physical_lines=physical_lines,
        logical_code_lines=logical_code_lines,
        comment_lines=comment_lines,
        blank_lines=blank_lines,
        parse_ok=False,
        parse_fallback_used=True,
        parse_error=detail,
        file_hash=file_hash,
        parse_confidence="low",
    )


def _full_file_analysis(path: Path, root: Node, grammar: LanguageGrammar, file_hash: str, line_count: int) -> FileAnalysis:
    """Build a fully-populated FileAnalysis from a successfully-parsed tree."""
    code_lines, comment_lines_set = _line_kinds_treesitter(root, grammar)
    blank_lines = line_count - len(code_lines | comment_lines_set)
    classes, top_level_functions = _walk_top_level(root, grammar)
    return FileAnalysis(
        path=str(path),
        relpath=path.name,
        language=grammar.language_name,
        physical_lines=line_count,
        logical_code_lines=len(code_lines),
        comment_lines=len(comment_lines_set),
        blank_lines=blank_lines,
        classes=classes,
        top_level_functions=top_level_functions,
        imports=_collect_imports(root, grammar),
        parse_ok=True,
        parse_fallback_used=False,
        file_hash=file_hash,
    )


#: Node types worth showing in an outline: control flow and scopes. An
#: expression statement or a declaration is noise at this altitude -- the
#: question an outline answers is "what is the shape of this function",
#: and the answer is its branches and loops, not its assignments.
_OUTLINE_SUFFIXES = (
    "_statement",
    "_clause",
    "_definition",
    "_declaration",
)
_OUTLINE_SKIP = {
    "expression_statement",
    "declaration",
    "field_declaration",
    "labeled_statement",
    "compound_statement",
}


def outline_with_grammar(
    source: str, start_line: int, end_line: int, grammar: LanguageGrammar, max_depth: int = 2
) -> list[dict] | None:
    """Control-flow outline of `source`'s [start_line, end_line] span.

    Written for the case that motivated it: a 703-line function whose shape
    had to be understood before it could be split. Reading it cost ~7k
    tokens; its outline is about sixty lines, and it is the outline -- not
    the bodies -- that says where the seams are.
    """
    parser = Parser(grammar.ts_language)
    tree = parser.parse(source.encode("utf-8", errors="replace"))
    lines = source.splitlines()
    entries: list[dict] = []

    def visit(node, depth: int) -> None:
        for child in node.children:
            child_start = child.start_point[0] + 1
            child_end = child.end_point[0] + 1
            if child_end < start_line or child_start > end_line:
                continue
            interesting = (
                child.type not in _OUTLINE_SKIP
                and any(child.type.endswith(suffix) for suffix in _OUTLINE_SUFFIXES)
                and child_end > child_start
            )
            if interesting:
                text = lines[child_start - 1].strip() if child_start - 1 < len(lines) else ""
                entries.append(
                    {
                        "type": child.type,
                        "start_line": child_start,
                        "end_line": child_end,
                        "lines": child_end - child_start + 1,
                        "depth": depth,
                        "text": text[:120],
                    }
                )
                if depth < max_depth:
                    visit(child, depth + 1)
            else:
                # Not interesting itself (a block, a scope): look inside it
                # at the SAME depth, so `{ ... }` does not cost a level.
                visit(child, depth)

    visit(tree.root_node, 0)
    # A construct and its own body can span the same lines (an `if` whose
    # braces start on the condition's line), which listed it twice. The
    # outline is a shape, and the same shape twice is noise.
    seen: set[tuple[int, int]] = set()
    unique = []
    for entry in entries:
        key = (entry["start_line"], entry["end_line"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(entry)
    return unique or None


def blocks_containing_with_grammar(
    source: str, line: int, grammar: LanguageGrammar
) -> list[dict] | None:
    """Every node whose line span contains `line`, outermost first.

    The parse tree already knows exactly where each block starts and ends,
    so no caller ever has to count braces to slice one out -- which is the
    operation that silently drops a closing brace or takes an opening one
    twice.
    """
    parser = Parser(grammar.ts_language)
    tree = parser.parse(source.encode("utf-8", errors="replace"))

    chain: list[dict] = []
    node = tree.root_node
    while node is not None:
        start, end = node.start_point[0] + 1, node.end_point[0] + 1
        if not (start <= line <= end):
            break
        chain.append({"type": node.type, "start_line": start, "end_line": end})
        next_node = None
        for child in node.children:
            child_start, child_end = child.start_point[0] + 1, child.end_point[0] + 1
            if child_start <= line <= child_end:
                next_node = child
                break
        node = next_node
    return chain or None


def analyze_with_grammar(path: Path, source: str, grammar: LanguageGrammar) -> FileAnalysis:
    """Analyze one source file using the shared tree-sitter engine.

    Falls back to a regex/brace-count LOC-only analysis (with
    `parse_fallback_used=True` and no violations crashing the run) when the
    parser reports a syntax error anywhere in the tree.
    """
    source_bytes = source.encode("utf-8", errors="replace")
    file_hash = hashlib.blake2b(source_bytes).hexdigest()
    parser = Parser(grammar.ts_language)
    tree = parser.parse(source_bytes)

    if tree.root_node.has_error:
        return _fallback_file_analysis(
            path, source, grammar, file_hash, _first_error_location(tree.root_node, source)
        )

    return _full_file_analysis(path, tree.root_node, grammar, file_hash, len(source.splitlines()))
