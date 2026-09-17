"""Python language adapter: stdlib `ast` for structure, `tokenize` for LOC/tokens.

No external dependency — Python is treated as a first-class language per
fixme.md §4.5, using only the standard library. Ported unchanged (besides
import paths) from `tools/code_quality/code_quality/adapters/python_adapter.py`.
"""

import ast
import hashlib
import io
import keyword
import tokenize
from pathlib import Path

from code_intelligence.core.parser.base import LanguageAdapter
from code_intelligence.core.parser.ir import (
    ClassInfo,
    FileAnalysis,
    FunctionInfo,
    IdentifierRef,
    ImportInfo,
    Token,
)

_COMPOUND_NESTING_NODES = (
    ast.If,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.Try,
    ast.With,
    ast.AsyncWith,
)


def _line_kinds(source: str) -> tuple[set[int], set[int]]:
    """Classify each physical line as code and/or comment via `tokenize`.

    Returns:
        A tuple ``(code_lines, comment_lines)`` of 1-based line numbers.
        A line can only be a "comment line" if it has no code token; a line
        with code plus a trailing comment counts as code.
    """
    code_lines: set[int] = set()
    comment_lines: set[int] = set()
    skip = {
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
    }
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            ttype, _tstring, start, end, _line = tok
            if ttype == tokenize.COMMENT:
                comment_lines.add(start[0])
            elif ttype not in skip:
                for lineno in range(start[0], end[0] + 1):
                    code_lines.add(lineno)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    comment_lines -= code_lines
    return code_lines, comment_lines


def _compute_loc(source: str) -> tuple[int, int, int, int]:
    """Return (physical_lines, logical_code_lines, comment_lines, blank_lines)."""
    lines = source.splitlines()
    physical_lines = len(lines)
    code_lines, comment_lines = _line_kinds(source)
    blank_lines = 0
    for i, line in enumerate(lines, start=1):
        if i in code_lines or i in comment_lines:
            continue
        if line.strip() == "":
            blank_lines += 1
    return physical_lines, len(code_lines), len(comment_lines), blank_lines


def _tokenize_range(source: str, start_line: int, end_line: int) -> list[Token]:
    """Build a normalized token_stream for the given inclusive line range."""
    stream: list[Token] = []
    skip = {
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENCODING,
        tokenize.ENDMARKER,
        tokenize.COMMENT,
    }
    try:
        tokens = tokenize.generate_tokens(io.StringIO(source).readline)
        for tok in tokens:
            ttype, tstring, start, _end, _line = tok
            if start[0] < start_line or start[0] > end_line:
                continue
            if ttype in skip:
                continue
            if ttype == tokenize.NAME:
                kind = "KEYWORD" if keyword.iskeyword(tstring) else "IDENT"
            elif ttype in (tokenize.NUMBER, tokenize.STRING, tokenize.FSTRING_START):
                kind = "LITERAL"
            elif ttype == tokenize.OP:
                kind = "OP"
            else:
                continue
            stream.append((kind, tstring))
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return stream


def _max_nesting_depth(node: ast.AST) -> int:
    """Compute the deepest compound-statement nesting reached under `node`."""
    best = 0

    def walk(n: ast.AST, depth: int) -> None:
        nonlocal best
        best = max(best, depth)
        for child in ast.iter_child_nodes(n):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue  # nested scopes get their own depth accounting
            if isinstance(child, _COMPOUND_NESTING_NODES):
                walk(child, depth + 1)
            else:
                walk(child, depth)

    walk(node, 0)
    return best


#: Node types that each add a flat +1 to cyclomatic complexity.
_SIMPLE_DECISION_TYPES = (ast.If, ast.For, ast.AsyncFor, ast.While)

#: Nested scopes get their own complexity accounting, so the walk stops here.
_NESTED_SCOPE_TYPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)


def _decision_weight(child: ast.AST) -> int:
    """Return how much `child` contributes to cyclomatic complexity."""
    if isinstance(child, _SIMPLE_DECISION_TYPES):
        return 1
    if isinstance(child, ast.Try):
        return len(child.handlers)
    if isinstance(child, ast.BoolOp):
        return max(len(child.values) - 1, 0)
    if isinstance(child, ast.comprehension):
        return len(child.ifs)
    if isinstance(child, ast.Match):
        return len(child.cases)
    return 0


def _cyclomatic_complexity(node: ast.AST) -> int:
    """McCabe-style complexity: 1 + count of decision points."""
    complexity = 1

    def walk(current: ast.AST) -> None:
        nonlocal complexity
        for child in ast.iter_child_nodes(current):
            if isinstance(child, _NESTED_SCOPE_TYPES):
                continue
            complexity += _decision_weight(child)
            walk(child)

    walk(node)
    return complexity


def _call_reference_name(func_expr: ast.expr) -> str | None:
    """Extract the callee/instantiated-type name from a `Call.func` expression.

    Handles a direct call (`helper(...)`, `func` is a plain `Name`) and a
    member call (`obj.method(...)`, `func` is an `Attribute` — the actually
    -invoked name is `.attr`). Anything else (subscripted calls, calls on
    call results, etc.) is skipped rather than guessed at.
    """
    if isinstance(func_expr, ast.Name):
        return func_expr.id
    if isinstance(func_expr, ast.Attribute):
        return func_expr.attr
    return None


def _call_reference_kind(name: str) -> str:
    """Best-effort call-vs-instantiation guess: Python has no `new` keyword,
    so this uses the CapWords convention (PEP 8) as a soft heuristic — it
    only affects the `kind` label used to build the callgraph index, never
    correctness of name-matching itself."""
    return "class" if name[:1].isupper() else "function"


def _collect_identifiers(node: ast.AST, args: ast.arguments) -> list[IdentifierRef]:
    """Collect parameter, loop-index and assigned-variable identifiers in a function body."""
    identifiers: list[IdentifierRef] = []

    for group in (args.posonlyargs, args.args, args.kwonlyargs):
        for param in group:
            identifiers.append(
                IdentifierRef(name=param.arg, kind="parameter", line=param.lineno, column=param.col_offset + 1)
            )
    if args.vararg:
        identifiers.append(
            IdentifierRef(
                name=args.vararg.arg, kind="parameter", line=args.vararg.lineno, column=args.vararg.col_offset + 1
            )
        )
    if args.kwarg:
        identifiers.append(
            IdentifierRef(
                name=args.kwarg.arg, kind="parameter", line=args.kwarg.lineno, column=args.kwarg.col_offset + 1
            )
        )

    class Visitor(ast.NodeVisitor):
        def visit_For(self, n: ast.For) -> None:
            for target in ast.walk(n.target):
                if isinstance(target, ast.Name):
                    identifiers.append(
                        IdentifierRef(
                            name=target.id,
                            kind="loop_index",
                            line=n.lineno,
                            is_exception=True,
                            column=target.col_offset + 1,
                        )
                    )
            self.generic_visit(n)

        def visit_Assign(self, n: ast.Assign) -> None:
            for target in n.targets:
                for name in ast.walk(target):
                    if isinstance(name, ast.Name):
                        identifiers.append(
                            IdentifierRef(
                                name=name.id, kind="variable", line=n.lineno, column=name.col_offset + 1
                            )
                        )
            self.generic_visit(n)

        def visit_Call(self, n: ast.Call) -> None:
            name = _call_reference_name(n.func)
            if name:
                identifiers.append(
                    IdentifierRef(
                        name=name,
                        kind=_call_reference_kind(name),
                        line=n.lineno,
                        column=n.col_offset + 1,
                    )
                )
            self.generic_visit(n)

        def visit_FunctionDef(self, n: ast.FunctionDef) -> None:
            pass  # nested function's own identifiers belong to its own FunctionInfo

        def visit_AsyncFunctionDef(self, n: ast.AsyncFunctionDef) -> None:
            pass

    visitor = Visitor()
    visitor.generic_visit(node)  # descend into node's children, not node itself
    return identifiers


_ENUM_BASE_NAMES = {"Enum", "IntEnum", "Flag", "IntFlag", "StrEnum", "ReprEnum"}


def _is_enum_class(node: ast.ClassDef) -> bool:
    """Return True when `node` derives from one of the stdlib `enum` base classes.

    fixme.md §4.4 enumerates "classes, structs, interfaces" as the type
    definitions counted toward MULTIPLE_TYPE_DEFINITION/one-type-per-file —
    an enum (a fixed set of named constants) is a different construct and
    is intentionally excluded, matching that literal list.
    """
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in _ENUM_BASE_NAMES:
            return True
        if isinstance(base, ast.Attribute) and base.attr in _ENUM_BASE_NAMES:
            return True
    return False


def _body_span(node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef, fallback: tuple[int, int]) -> tuple[int, int]:
    """Return (body_start_line, body_end_line) from a node's statement list.

    Falls back to `fallback` (typically the node's own start/end line) when
    the body is empty or lacks line info — should not happen for valid
    parsed source, but keeps this defensive per the adapter contract of
    never raising on structurally odd input.
    """
    if not node.body:
        return fallback
    first, last = node.body[0], node.body[-1]
    body_start = getattr(first, "lineno", None)
    body_end = getattr(last, "end_lineno", None) or getattr(last, "lineno", None)
    if body_start is None or body_end is None:
        return fallback
    return body_start, body_end


def _build_function_info(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    source: str,
    owner_class: str | None,
) -> FunctionInfo:
    """Build a FunctionInfo for one function/method AST node."""
    start_line = node.lineno
    end_line = getattr(node, "end_lineno", None) or start_line
    start_column = node.col_offset + 1
    end_column = (getattr(node, "end_col_offset", None) or node.col_offset) + 1
    body_start_line, body_end_line = _body_span(node, fallback=(start_line, end_line))
    qualified_name = f"{owner_class}.{node.name}" if owner_class else node.name
    return FunctionInfo(
        name=node.name,
        qualified_name=qualified_name,
        start_line=start_line,
        end_line=end_line,
        physical_lines=end_line - start_line + 1,
        has_doc=ast.get_docstring(node) is not None,
        nesting_depth=_max_nesting_depth(node),
        cyclomatic_complexity=_cyclomatic_complexity(node),
        identifiers=_collect_identifiers(node, node.args),
        token_stream=_tokenize_range(source, start_line, end_line),
        is_method=owner_class is not None,
        owner_class=owner_class,
        start_column=start_column,
        end_column=end_column,
        body_start_line=body_start_line,
        body_end_line=body_end_line,
    )


def _build_class_info(node: ast.ClassDef, source: str) -> ClassInfo:
    """Build a ClassInfo for one class AST node, including its methods."""
    end_line = getattr(node, "end_lineno", None) or node.lineno
    start_column = node.col_offset + 1
    end_column = (getattr(node, "end_col_offset", None) or node.col_offset) + 1
    methods = [
        _build_function_info(child, source, owner_class=node.name)
        for child in node.body
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return ClassInfo(
        name=node.name,
        kind="class",
        start_line=node.lineno,
        end_line=end_line,
        has_doc=ast.get_docstring(node) is not None,
        methods=methods,
        start_column=start_column,
        end_column=end_column,
    )


def _collect_imports(tree: ast.Module) -> list[ImportInfo]:
    """Collect top-level and nested import statements."""
    imports: list[ImportInfo] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(ImportInfo(module=alias.name, line=node.lineno))
        elif isinstance(node, ast.ImportFrom):
            module = ("." * node.level) + (node.module or "")
            imports.append(ImportInfo(module=module, line=node.lineno))
    return imports


def _parse_fallback_analysis(path: Path, loc_counts: tuple[int, int, int, int], file_hash: str, error: Exception) -> FileAnalysis:
    """Build a LOC-only FileAnalysis for a file that failed to parse."""
    physical_lines, logical_code_lines, comment_lines, blank_lines = loc_counts
    return FileAnalysis(
        path=str(path),
        relpath=path.name,
        language="python",
        physical_lines=physical_lines,
        logical_code_lines=logical_code_lines,
        comment_lines=comment_lines,
        blank_lines=blank_lines,
        parse_ok=False,
        parse_fallback_used=True,
        parse_error=str(error),
        file_hash=file_hash,
        parse_confidence="low",
    )


def _full_analysis(path: Path, source: str, tree: ast.Module, loc_counts: tuple[int, int, int, int], file_hash: str) -> FileAnalysis:
    """Build a fully-populated FileAnalysis from a successfully-parsed module."""
    physical_lines, logical_code_lines, comment_lines, blank_lines = loc_counts
    classes = [
        _build_class_info(node, source)
        for node in tree.body
        if isinstance(node, ast.ClassDef) and not _is_enum_class(node)
    ]
    top_level_functions = [
        _build_function_info(node, source, owner_class=None)
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return FileAnalysis(
        path=str(path),
        relpath=path.name,
        language="python",
        physical_lines=physical_lines,
        logical_code_lines=logical_code_lines,
        comment_lines=comment_lines,
        blank_lines=blank_lines,
        classes=classes,
        top_level_functions=top_level_functions,
        imports=_collect_imports(tree),
        parse_ok=True,
        parse_fallback_used=False,
        file_hash=file_hash,
    )


class PythonAdapter(LanguageAdapter):
    """Adapter for `.py` source using stdlib `ast` + `tokenize`."""

    language = "python"
    extensions = (".py", ".pyi")

    def analyze(self, path: Path, source: str) -> FileAnalysis:
        """Parse Python source into the common IR, falling back on SyntaxError."""
        loc_counts = _compute_loc(source)
        file_hash = hashlib.blake2b(source.encode("utf-8", errors="replace")).hexdigest()

        try:
            tree = ast.parse(source, filename=str(path))
        except (SyntaxError, ValueError) as exc:
            return _parse_fallback_analysis(path, loc_counts, file_hash, exc)

        return _full_analysis(path, source, tree, loc_counts, file_hash)
