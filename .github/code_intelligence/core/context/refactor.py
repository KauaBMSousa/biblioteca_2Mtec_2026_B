"""Semantic refactoring primitives (ENHANCEME §F, §W).

The agent states the refactoring; the machine does the mechanical work and
returns counts. Everything here goes through the Phase 2 transaction
engine, so each primitive is snapshot / apply / reindex / validate /
commit-or-roll-back, and none of them return rewritten source.

Currently Python-only, and deliberately conservative:

* `add_import` — LibCST's `AddImportsVisitor` places and de-duplicates the
  import; a no-op when it is already there.
* `organize_imports` / `remove_unused_imports` — ruff's isort + pyflakes
  fixers over a scope, wrapped in the same validate/rollback envelope.
* `extract_function` — stdlib `ast` computes the free variables, the
  parameters and the return values; the statements move to a new
  top-level function and the range becomes a call. It **refuses** (with a
  `confidence` of `low`/`unknown` and a reason) rather than guess when the
  selection escapes its enclosing scope — `return`/`yield`/`await`,
  `global`/`nonlocal`, a `break`/`continue` with no loop of its own, or a
  selection that is not a run of complete statements. That refusal is the
  point: §Limitations calls for exactly this.

C++ / Java / PHP / JavaScript raise `UnsupportedLanguageError` here for
now and should fall back to `ast_transform`.
"""

import ast
import builtins
from typing import Any

from code_intelligence.core import exec as exec_module
from code_intelligence.core.context.transaction import _diag_count, _restore  # shared tx internals
from code_intelligence.core.context.transaction import finalize_change, snapshot_files
from code_intelligence.core.exec.errors import UnsupportedLanguageError

_BUILTINS = set(dir(builtins))


# -- imports ----------------------------------------------------------------


def add_import(
    workspace: Any,
    file: str,
    module: str,
    name: str | None = None,
    alias: str | None = None,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Add `from module import name` (or `import module`) to `file`, placed and de-duplicated.

    `name=None` adds a plain `import module`. Returns `{outcome:
    "no_change"}` when the import is already present. Python only.
    """
    row = workspace.store.get_file(file)
    if row is None or row["language"] != "python":
        raise UnsupportedLanguageError(f"add_import supports Python files only (got {file!r})")

    try:
        import libcst as cst
        from libcst.codemod import CodemodContext
        from libcst.codemod.visitors import AddImportsVisitor
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise UnsupportedLanguageError(
            f"add_import needs the libcst package importable: {exc}"
        ) from exc

    source = (workspace.root / file).read_text(encoding="utf-8")
    context = CodemodContext()
    AddImportsVisitor.add_needed_import(context, module, name, asname=alias)
    tree = cst.parse_module(source)
    new_source = AddImportsVisitor(context).transform_module(tree).code

    if new_source == source:
        return {"outcome": "no_change", "committed": False, "rolled_back": False, "file": file}

    return _run_file_rewrite(
        workspace, file, new_source, validate or ["syntax"], commit,
        extra={"import": f"{module}.{name}" if name else module},
    )


def organize_imports(
    workspace: Any,
    scope: str | None = None,
    *,
    select: str = "I,F401",
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Sort imports and drop unused ones across `scope`, via ruff — as one transaction."""
    root = workspace.root
    store = workspace.store
    py_files = {
        r["path"]
        for r in store.list_files()
        if r["language"] == "python" and (scope is None or r["path"].startswith(scope))
    }
    if not py_files:
        return {"outcome": "no_matches", "committed": False, "rolled_back": False}

    snapshot = snapshot_files(root, py_files)
    diags_before = _diag_count(store, py_files)

    result = exec_module.run_import_fix(root, scope, select)
    if result["status"] == "FAILED":
        _restore(root, snapshot)
        workspace.index()
        return {
            "outcome": "apply_failed",
            "committed": False,
            "rolled_back": True,
            "error": result.get("log_tail"),
        }

    return finalize_change(
        workspace,
        snapshot,
        py_files,
        diags_before,
        validate or ["syntax"],
        commit,
        extra={"fixed": result["fixed"], "remaining_issues": result["remaining_issues"]},
    )


def remove_unused_imports(
    workspace: Any,
    scope: str | None = None,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Drop unused imports across `scope` (ruff F401) — as one transaction."""
    return organize_imports(
        workspace, scope, select="F401", validate=validate, commit=commit
    )


# -- extract_function -----------------------------------------------------------


def _names(node: ast.AST, ctx: type) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name) and isinstance(n.ctx, ctx)}


def _enclosing_funcdef(tree: ast.Module, start_line: int, end_line: int) -> ast.AST | None:
    best = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.body and node.body[0].lineno <= start_line and node.body[-1].end_lineno >= end_line:
            if best is None or node.lineno > best.lineno:
                best = node
    return best


def _escapes(block_nodes: list[ast.stmt]) -> str | None:
    """A reason the selection cannot be safely extracted, or None if it is safe."""
    all_nodes = [node for stmt in block_nodes for node in ast.walk(stmt)]
    for node in all_nodes:
        if isinstance(node, (ast.Return, ast.Yield, ast.YieldFrom, ast.Await)):
            return f"selection contains {type(node).__name__.lower()} — it escapes the function"
        if isinstance(node, (ast.Global, ast.Nonlocal)):
            return "selection contains a global/nonlocal declaration"
    has_own_loop = any(isinstance(n, (ast.For, ast.While, ast.AsyncFor)) for n in all_nodes)
    has_break = any(isinstance(n, (ast.Break, ast.Continue)) for n in all_nodes)
    if has_break and not has_own_loop:
        return "selection contains a break/continue with no loop of its own"
    return None


def extract_function(
    workspace: Any,
    file: str,
    start_line: int,
    end_line: int,
    new_name: str,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Extract lines `[start_line, end_line]` of `file` into a new top-level function `new_name`.

    Computes the parameters (free variables read in the selection),
    the return values (names assigned in the selection and used after it),
    creates `def new_name(...)`, and replaces the selection with a call.
    Refuses with `confidence` `low`/`unknown` when the selection escapes
    its scope. Python only.
    """
    row = workspace.store.get_file(file)
    if row is None or row["language"] != "python":
        raise UnsupportedLanguageError(f"extract_function supports Python files only (got {file!r})")
    if not new_name.isidentifier():
        raise ValueError(f"{new_name!r} is not a valid identifier")

    source = (workspace.root / file).read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    func = _enclosing_funcdef(tree, start_line, end_line)
    if func is None:
        return _refuse("no single function encloses the selection", "unknown")

    block_nodes = [
        s for s in func.body if s.lineno >= start_line and s.end_lineno <= end_line
    ]
    if not block_nodes:
        return _refuse("the selection is not a run of complete statements in one function", "unknown")
    selected_span = (block_nodes[0].lineno, block_nodes[-1].end_lineno)

    escape_reason = _escapes(block_nodes)
    if escape_reason:
        return _refuse(escape_reason, "low")

    block_src = "\n".join(lines[selected_span[0] - 1 : selected_span[1]])
    block_module = ast.parse(_dedent(block_src))

    loaded_in = set()
    stored_in = set()
    for stmt in block_module.body:
        loaded_in |= _names(stmt, ast.Load)
        stored_in |= _names(stmt, ast.Store)

    arg_names = {a.arg for a in func.args.args} | {a.arg for a in func.args.kwonlyargs}
    if func.args.vararg:
        arg_names.add(func.args.vararg.arg)
    if func.args.kwarg:
        arg_names.add(func.args.kwarg.arg)

    assigned_before: set[str] = set()
    loaded_after: set[str] = set()
    for stmt in func.body:
        if stmt.end_lineno < selected_span[0]:
            assigned_before |= _names(stmt, ast.Store)
        elif stmt.lineno > selected_span[1]:
            loaded_after |= _names(stmt, ast.Load)

    params = sorted((loaded_in & (assigned_before | arg_names)) - _BUILTINS)
    returns = sorted((stored_in & loaded_after) - _BUILTINS)

    indent = _leading_ws(lines[selected_span[0] - 1])
    new_func_lines = [f"def {new_name}({', '.join(params)}):"]
    for bline in _dedent(block_src).splitlines():
        new_func_lines.append(f"    {bline}" if bline.strip() else "")
    if returns:
        new_func_lines.append(f"    return {', '.join(returns)}")

    call = f"{new_name}({', '.join(params)})"
    if returns:
        call = f"{', '.join(returns)} = {call}"
    call_line = f"{indent}{call}"

    # splice: new function immediately before the enclosing function, at its indent
    func_indent = _leading_ws(lines[func.lineno - 1])
    new_func_block = [f"{func_indent}{l}" if l else "" for l in new_func_lines]

    updated = (
        lines[: func.lineno - 1]
        + new_func_block
        + [""]
        + lines[func.lineno - 1 : selected_span[0] - 1]
        + [call_line]
        + lines[selected_span[1] :]
    )
    new_source = "\n".join(updated) + ("\n" if source.endswith("\n") else "")

    return _run_file_rewrite(
        workspace, file, new_source, validate or ["syntax", "tests"], commit,
        extra={
            "new_symbol": new_name,
            "parameters": params,
            "returns": returns,
            "confidence": "medium",
            "extracted_lines": list(selected_span),
        },
    )


# -- helpers ------------------------------------------------------------------


def _leading_ws(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _dedent(text: str) -> str:
    lines = [l for l in text.splitlines() if l.strip()]
    if not lines:
        return text
    common = min(len(_leading_ws(l)) for l in lines)
    return "\n".join(l[common:] if l.strip() else "" for l in text.splitlines())


def _funcdef_by_name(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _plain_params(func: ast.FunctionDef) -> list[str] | None:
    """The parameter names, or None when the signature is too complex to touch safely."""
    a = func.args
    if a.vararg or a.kwarg or a.kwonlyargs or a.posonlyargs or a.defaults or a.kw_defaults:
        return None
    if func.decorator_list:
        return None
    return [arg.arg for arg in a.args]


def change_signature(
    workspace: Any,
    file: str,
    symbol: str,
    add_parameter: dict[str, Any] | None = None,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Append a parameter (with a default) to a top-level Python function.

    Only the purely-additive case: a new trailing parameter with a default
    leaves every existing call valid, so no call site is touched. Anything
    else (`change_signature` reordering, removal, `rename_parameter`,
    methods) is refused for now — `outcome: "refused"`.
    """
    row = workspace.store.get_file(file)
    if row is None or row["language"] != "python":
        raise UnsupportedLanguageError(f"change_signature supports Python files only (got {file!r})")
    if not add_parameter:
        return _refuse("only add_parameter is supported so far", "unknown")

    name = add_parameter["name"]
    default = add_parameter.get("default", "None")
    if not name.isidentifier():
        raise ValueError(f"{name!r} is not a valid identifier")

    source = (workspace.root / file).read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)
    func = _funcdef_by_name(tree, symbol.split(".")[-1])
    if func is None:
        return _refuse(f"{symbol!r} is not a top-level function in {file}", "unknown")
    if _plain_params(func) is None:
        return _refuse("signature has *args/**kwargs/defaults/decorators — not touched", "low")
    if any(p == name for p in _plain_params(func)):
        return _refuse(f"{name!r} is already a parameter", "unknown")

    def_lineno = func.lineno
    def_end = func.body[0].lineno - 1  # signature may span lines
    sig_text = "\n".join(lines[def_lineno - 1 : def_end])
    close = sig_text.rfind(")")
    if close == -1:
        return _refuse("could not locate the parameter list", "unknown")
    existing = _plain_params(func)
    insert = f"{name}={default}"
    new_sig = sig_text[:close].rstrip()
    new_sig += (", " if existing else "") + insert + sig_text[close:]
    updated = lines[: def_lineno - 1] + new_sig.splitlines() + lines[def_end:]
    new_source = "\n".join(updated) + ("\n" if source.endswith("\n") else "")

    return _run_file_rewrite(
        workspace, file, new_source, validate or ["syntax"], commit,
        extra={"added_parameter": name, "confidence": "high"},
    )


def inline_function(
    workspace: Any,
    file: str,
    symbol: str,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Inline a trivial top-level Python function (`return <expr>`) into its call sites.

    Conservative: the function body must be exactly one `return`
    expression, the parameter list must be plain (no defaults / *args /
    **kwargs / decorators), and the file must have exact semantic
    coverage. Every call site must be a positional call with the right
    arity. If any of that does not hold it refuses (`outcome: "refused"`)
    and nothing is written.
    """
    from code_intelligence.core.index import semantic_store

    row = workspace.store.get_file(file)
    if row is None or row["language"] != "python":
        raise UnsupportedLanguageError(f"inline_function supports Python files only (got {file!r})")

    store = workspace.store
    unit = store.connection.execute(
        "SELECT ok FROM semantic_units WHERE unit = ?", (file,)
    ).fetchone()
    if not (unit and unit["ok"]):
        return _refuse(f"{file} has no exact semantic coverage — cannot find every call site", "low")

    source = (workspace.root / file).read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = _funcdef_by_name(tree, symbol.split(".")[-1])
    if func is None:
        return _refuse(f"{symbol!r} is not a top-level function in {file}", "unknown")
    params = _plain_params(func)
    if params is None:
        return _refuse("signature too complex to inline", "low")
    if len(func.body) != 1 or not isinstance(func.body[0], ast.Return) or func.body[0].value is None:
        return _refuse("function body is not a single `return <expr>`", "low")

    body_expr_src = ast.get_source_segment(source, func.body[0].value)
    edges = semantic_store.references_to(store, file, func.lineno)
    if not edges:
        return _refuse("no recorded call sites — nothing to inline into", "medium")

    by_file: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        by_file.setdefault(edge["from_file"], []).append(edge)

    new_text_by_file: dict[str, str] = {}
    sites_rewritten = 0
    for call_file, site_edges in by_file.items():
        call_source = (workspace.root / call_file).read_text(encoding="utf-8")
        call_tree = ast.parse(call_source)
        lines_to_calls: dict[int, list[ast.Call]] = {}
        for node in ast.walk(call_tree):
            if isinstance(node, ast.Call) and _call_name(node.func) == func.name:
                lines_to_calls.setdefault(node.lineno, []).append(node)

        replacements: list[tuple[int, int, int, int, str]] = []
        for edge in site_edges:
            calls = lines_to_calls.get(edge["from_line"], [])
            if len(calls) != 1:
                return _refuse(
                    f"{call_file}:{edge['from_line']} has {len(calls)} calls to {func.name!r} — ambiguous",
                    "low",
                )
            call = calls[0]
            if call.keywords or len(call.args) != len(params):
                return _refuse(
                    f"{call_file}:{edge['from_line']} is not a positional call with {len(params)} args",
                    "low",
                )
            arg_srcs = [ast.get_source_segment(call_source, a) for a in call.args]
            inlined = _substitute_params(body_expr_src, params, arg_srcs)
            replacements.append(
                (call.lineno, call.col_offset, call.end_lineno, call.end_col_offset, f"({inlined})")
            )

        new_text_by_file[call_file] = _apply_span_replacements(call_source, replacements)
        sites_rewritten += len(replacements)

    # Remove the definition. If its own file also holds call sites, remove it
    # from the already-call-rewritten text so the two edits do not collide.
    def_file_text = new_text_by_file.get(file, source)
    def_tree = ast.parse(def_file_text)
    def_node = _funcdef_by_name(def_tree, func.name)
    new_text_by_file[file] = (
        _remove_funcdef(def_file_text, def_node) if def_node is not None else def_file_text
    )

    operations = [
        {"op": "write_file", "file": relpath, "new_text": text}
        for relpath, text in new_text_by_file.items()
    ]

    result = workspace.execute_transaction(
        operations, validate=validate or ["syntax", "tests"], commit=commit
    )
    result.update({"inlined_call_sites": sites_rewritten, "confidence": "medium"})
    return result


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _substitute_params(expr_src: str, params: list[str], args: list[str]) -> str:
    """Replace each bare `param` name in `expr_src` with `(arg)` (parenthesized for safety)."""
    mapping = {p: f"({a})" for p, a in zip(params, args)}

    class _Sub(ast.NodeTransformer):
        def visit_Name(self, node: ast.Name) -> ast.AST:
            if isinstance(node.ctx, ast.Load) and node.id in mapping:
                return ast.copy_location(ast.parse(mapping[node.id], mode="eval").body, node)
            return node

    tree = ast.parse(expr_src, mode="eval")
    return ast.unparse(_Sub().visit(tree))


def _apply_span_replacements(
    source: str, replacements: list[tuple[int, int, int, int, str]]
) -> str:
    """Apply `(start_line, start_col, end_line, end_col, text)` replacements, last-first."""
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def pos(line: int, col: int) -> int:
        return offsets[line - 1] + col

    spans = sorted(replacements, key=lambda r: pos(r[0], r[1]), reverse=True)
    out = source
    for sl, sc, el, ec, text in spans:
        out = out[: pos(sl, sc)] + text + out[pos(el, ec) :]
    return out


def _remove_funcdef(source: str, func: ast.FunctionDef) -> str:
    """Return `source` with `func`'s whole definition removed."""
    lines = source.splitlines()
    start = func.lineno - 1
    end = func.end_lineno
    del lines[start:end]
    # collapse a run of blank lines left behind
    while start < len(lines) and not lines[start].strip() and (start == 0 or not lines[start - 1].strip()):
        del lines[start]
    return "\n".join(lines) + ("\n" if source.endswith("\n") else "")


def _refuse(reason: str, confidence: str) -> dict[str, Any]:
    return {
        "outcome": "refused",
        "committed": False,
        "rolled_back": False,
        "confidence": confidence,
        "reason": reason,
    }


def _run_file_rewrite(
    workspace: Any,
    file: str,
    new_source: str,
    validate: list[str],
    commit: bool,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply a whole-file rewrite through the transaction engine."""
    result = workspace.execute_transaction(
        [{"op": "write_file", "file": file, "new_text": new_source}],
        validate=validate,
        commit=commit,
    )
    if extra:
        result.update(extra)
    return result


__all__ = [
    "add_import",
    "organize_imports",
    "remove_unused_imports",
    "extract_function",
    "change_signature",
    "inline_function",
]
