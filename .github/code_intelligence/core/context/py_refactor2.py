"""Python cross-scope refactors: rename_parameter, change_signature, move_symbol.

The three §F/§W primitives that touch more than one place at once. Each
resolves its edit sites from the exact semantic index (jedi-backed
`semantic_edges`) rather than name matching, applies every change through
the transaction engine, and refuses — with a `confidence` and a `reason` —
when a site is ambiguous or a change would drop data. Python only.
"""

import ast
from typing import Any

from code_intelligence.core.context.refactor import _apply_span_replacements, _refuse
from code_intelligence.core.exec.errors import UnsupportedLanguageError
from code_intelligence.core.index import semantic_store

_TRIVIAL_ARG = {"None", "True", "False", "0", "1", "''", '""', "[]", "{}", "()"}


def _module_path(relpath: str) -> str:
    stem = relpath[:-3] if relpath.endswith(".py") else relpath
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    return stem.replace("/", ".")


def _funcdef(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Find a function by (possibly dotted) name, top-level or one class deep."""
    tail = name.split(".")[-1]
    owner = name.split(".")[-2] if "." in name else None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == tail:
            if owner is None:
                return node
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef) and parent.name == owner and node in ast.walk(parent):
                    return node
    return None


def _param_names(func: ast.FunctionDef) -> list[str]:
    a = func.args
    return (
        [p.arg for p in a.posonlyargs]
        + [p.arg for p in a.args]
        + [p.arg for p in a.kwonlyargs]
    )


def _require_python(workspace: Any, *files: str) -> None:
    for file in files:
        row = workspace.store.get_file(file)
        if row is None or row["language"] != "python":
            raise UnsupportedLanguageError(f"Python files only (got {file!r})")


# -- rename_parameter -------------------------------------------------------


def rename_parameter(
    workspace: Any,
    file: str,
    symbol: str,
    old_name: str,
    new_name: str,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Rename a parameter in the definition, its body uses, and `name=` call-site keywords."""
    _require_python(workspace, file)
    if not new_name.isidentifier():
        raise ValueError(f"{new_name!r} is not a valid identifier")

    source = (workspace.root / file).read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = _funcdef(tree, symbol)
    if func is None:
        return _refuse(f"{symbol!r} not found in {file}", "unknown")
    params = _param_names(func)
    if old_name not in params:
        return _refuse(f"{old_name!r} is not a parameter of {symbol}", "unknown")
    if new_name in params:
        return _refuse(f"{new_name!r} is already a parameter", "unknown")

    # shadowing hazards inside the body -> refuse rather than rename wrongly
    for node in ast.walk(func):
        if node is func:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            inner = node.args
            if any(
                p.arg == old_name
                for p in inner.posonlyargs + inner.args + inner.kwonlyargs
            ):
                return _refuse(f"a nested scope rebinds {old_name!r}", "low")
        if isinstance(node, (ast.Global, ast.Nonlocal)) and old_name in node.names:
            return _refuse(f"{old_name!r} is declared global/nonlocal in the body", "low")

    lines = source.splitlines()
    edits: list[tuple[int, int, str]] = []  # (line, col, new_text) - single-token swaps

    for arg in func.args.posonlyargs + func.args.args + func.args.kwonlyargs:
        if arg.arg == old_name:
            edits.append((arg.lineno, arg.col_offset, new_name))
    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == old_name:
            edits.append((node.lineno, node.col_offset, new_name))

    for line_no, col, text in sorted(edits, key=lambda e: (e[0], -e[1])):
        row = lines[line_no - 1]
        lines[line_no - 1] = row[:col] + text + row[col + len(old_name) :]
    new_source = "\n".join(lines) + ("\n" if source.endswith("\n") else "")

    operations = [{"op": "write_file", "file": file, "new_text": new_source}]
    call_sites = _rewrite_call_keyword(workspace, file, func.lineno, func.name, old_name, new_name)
    operations.extend(call_sites["operations"])

    result = workspace.execute_transaction(
        operations, validate=validate or ["syntax", "tests"], commit=commit
    )
    result.update(
        {"renamed": f"{old_name} -> {new_name}", "call_sites_updated": call_sites["count"],
         "confidence": "high"}
    )
    return result


def _rewrite_call_keyword(
    workspace: Any, def_file: str, def_line: int, func_name: str, old_kw: str, new_kw: str
) -> dict[str, Any]:
    """Rewrite `func(old_kw=...)` to `func(new_kw=...)` at every recorded call site."""
    edges = semantic_store.references_to(workspace.store, def_file, def_line)
    by_file: dict[str, set[int]] = {}
    for edge in edges:
        by_file.setdefault(edge["from_file"], set()).add(edge["from_line"])

    operations: list[dict[str, Any]] = []
    count = 0
    for call_file, call_lines in by_file.items():
        text = (workspace.root / call_file).read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.splitlines()
        swaps: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or node.lineno not in call_lines:
                continue
            if _call_name(node.func) != func_name:
                continue
            for kw in node.keywords:
                if kw.arg == old_kw:
                    swaps.append((kw.value.lineno, kw.value.col_offset))
        if not swaps:
            continue
        # the keyword name sits just before `=` before the value; find it on the line
        for value_line, value_col in swaps:
            row = lines[value_line - 1]
            head = row[:value_col].rstrip()
            if head.endswith("="):
                kw_start = head[:-1].rstrip()
                idx = kw_start.rfind(old_kw)
                if idx >= 0 and kw_start[idx:] == old_kw:
                    lines[value_line - 1] = (
                        row[:idx] + new_kw + row[idx + len(old_kw) :]
                    )
                    count += 1
        operations.append(
            {"op": "write_file", "file": call_file, "new_text": "\n".join(lines) + ("\n" if text.endswith("\n") else "")}
        )
    return {"operations": operations, "count": count}


def _call_name(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# -- change_signature (full: reorder / add / remove) ----------------------


def change_signature_full(
    workspace: Any,
    file: str,
    symbol: str,
    parameters: list[dict[str, Any]],
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Set a function's full parameter list and update every call site to keyword form.

    `parameters` is the new ordered list of `{name, default?}`. Positional
    args at call sites are converted to keyword args (order-independent),
    so reordering is safe. Refuses when: an added parameter has no default,
    a removed parameter is passed a non-trivial value at some call site, or
    a call spreads `*args`/`**kwargs`.
    """
    _require_python(workspace, file)
    source = (workspace.root / file).read_text(encoding="utf-8")
    tree = ast.parse(source)
    func = _funcdef(tree, symbol)
    if func is None:
        return _refuse(f"{symbol!r} not found in {file}", "unknown")
    if func.args.vararg or func.args.kwarg or func.decorator_list:
        return _refuse("signature has *args/**kwargs/decorators", "low")

    old_params = _param_names(func)
    new_params = [p["name"] for p in parameters]
    if len(new_params) != len(set(new_params)):
        return _refuse("duplicate parameter names in the new signature", "unknown")

    added = [p for p in parameters if p["name"] not in old_params]
    removed = [p for p in old_params if p not in new_params]
    for param in added:
        if "default" not in param:
            return _refuse(f"added parameter {param['name']!r} needs a default", "low")

    is_method = _param_names_owner(tree, func) is not None
    self_param = old_params[0] if is_method and old_params else None

    # rewrite the definition
    lines = source.splitlines()
    def_end = func.body[0].lineno - 1
    sig_text = "\n".join(lines[func.lineno - 1 : def_end])
    open_paren = sig_text.index("(")
    close_paren = sig_text.rindex(")")
    rendered = []
    for param in parameters:
        rendered.append(f"{param['name']}={param['default']}" if "default" in param else param["name"])
    if self_param and self_param not in new_params:
        rendered.insert(0, self_param)
    new_sig = sig_text[: open_paren + 1] + ", ".join(rendered) + sig_text[close_paren:]
    updated = lines[: func.lineno - 1] + new_sig.splitlines() + lines[def_end:]
    def_source = "\n".join(updated) + ("\n" if source.endswith("\n") else "")

    operations = [{"op": "write_file", "file": file, "new_text": def_source}]

    # rewrite call sites to all-keyword form
    edges = semantic_store.references_to(workspace.store, file, func.lineno)
    by_file: dict[str, set[int]] = {}
    for edge in edges:
        by_file.setdefault(edge["from_file"], set()).add(edge["from_line"])

    call_params = [p for p in old_params if p != self_param]
    sites = 0
    for call_file, call_lines in by_file.items():
        ctext = (workspace.root / call_file).read_text(encoding="utf-8")
        ctree = ast.parse(ctext)
        replacements: list[tuple[int, int, int, int, str]] = []
        for node in ast.walk(ctree):
            if not isinstance(node, ast.Call) or node.lineno not in call_lines:
                continue
            if _call_name(node.func) != func.name:
                continue
            if any(isinstance(a, ast.Starred) for a in node.args) or any(k.arg is None for k in node.keywords):
                return _refuse(f"{call_file}:{node.lineno} spreads *args/**kwargs", "low")
            bound: dict[str, str] = {}
            for i, arg in enumerate(node.args):
                if i >= len(call_params):
                    return _refuse(f"{call_file}:{node.lineno} passes too many positional args", "low")
                bound[call_params[i]] = ast.get_source_segment(ctext, arg)
            for kw in node.keywords:
                bound[kw.arg] = ast.get_source_segment(ctext, kw.value)
            for name in removed:
                if name in bound and bound[name] not in _TRIVIAL_ARG:
                    return _refuse(
                        f"{call_file}:{node.lineno} passes {name!r}={bound[name]} — removing it would drop data",
                        "low",
                    )
                bound.pop(name, None)
            parts = [f"{p['name']}={bound[p['name']]}" for p in parameters if p["name"] in bound]
            func_src = ast.get_source_segment(ctext, node.func)
            new_call = f"{func_src}({', '.join(parts)})"
            replacements.append(
                (node.lineno, node.col_offset, node.end_lineno, node.end_col_offset, new_call)
            )
            sites += 1
        if replacements:
            operations.append(
                {"op": "write_file", "file": call_file, "new_text": _apply_span_replacements(ctext, replacements)}
            )

    result = workspace.execute_transaction(
        operations, validate=validate or ["syntax", "tests"], commit=commit
    )
    result.update(
        {"added": [p["name"] for p in added], "removed": removed,
         "call_sites_updated": sites, "confidence": "medium"}
    )
    return result


def _param_names_owner(tree: ast.AST, func: ast.FunctionDef) -> ast.ClassDef | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and func in node.body:
            return node
    return None


# -- move_symbol ----------------------------------------------------------


def move_symbol(
    workspace: Any,
    symbol: str,
    from_file: str,
    to_file: str,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Move a top-level function/class from `from_file` to `to_file`, repairing imports.

    Leaves a compatibility `from <to_module> import <symbol>` in
    `from_file` if it still references the symbol, rewrites
    `from <from_module> import <symbol>` in other files, and reports
    anything it could not resolve automatically in `needs_manual`.
    """
    _require_python(workspace, from_file)
    root = workspace.root
    src = (root / from_file).read_text(encoding="utf-8")
    tree = ast.parse(src)
    node = next(
        (
            n
            for n in tree.body
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and n.name == symbol.split(".")[-1]
        ),
        None,
    )
    if node is None:
        return _refuse(f"{symbol!r} is not a top-level symbol of {from_file}", "unknown")

    lines = src.splitlines()
    start = (node.decorator_list[0].lineno - 1) if node.decorator_list else (node.lineno - 1)
    end = node.end_lineno
    moved_src = "\n".join(lines[start:end])

    from_module = _module_path(from_file)
    to_module = _module_path(to_file)

    # module-level names the symbol depends on and that live in from_file
    top_defs = {
        n.name
        for n in tree.body
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    } - {node.name}
    top_assigns = {
        t.id
        for n in tree.body
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Name)
    }
    used = {x.id for x in ast.walk(node) if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
    back_imports = sorted((used & (top_defs | top_assigns)))

    # remove from source, collapse trailing blanks
    del lines[start:end]
    while start < len(lines) and not lines[start].strip() and (start == 0 or not lines[start - 1].strip()):
        del lines[start]
    remaining_src = "\n".join(lines).rstrip("\n")
    still_uses = symbol.split(".")[-1] in {
        x.id for x in ast.walk(ast.parse(remaining_src or "")) if isinstance(x, ast.Name)
    }
    new_from_lines = remaining_src.splitlines()
    if still_uses:
        new_from_lines = _insert_import(new_from_lines, to_module, node.name)
    from_new = "\n".join(new_from_lines) + "\n"

    # target file
    to_path = root / to_file
    to_src = to_path.read_text(encoding="utf-8") if to_path.exists() else '"""' + to_module + '."""\n'
    to_lines = to_src.splitlines()
    for name in back_imports:
        to_lines = _insert_import(to_lines, from_module, name)
    to_new = "\n".join(to_lines).rstrip("\n") + "\n\n\n" + moved_src + "\n"

    operations = [
        {"op": "write_file", "file": from_file, "new_text": from_new},
        {"op": "write_file", "file": to_file, "new_text": to_new},
    ]

    # rewrite importers
    needs_manual: list[str] = []
    importers_updated = 0
    for file_row in workspace.store.list_files():
        relpath = file_row["path"]
        if file_row["language"] != "python" or relpath in (from_file, to_file):
            continue
        ftext = (root / relpath).read_text(encoding="utf-8")
        ftree = ast.parse(ftext)
        flines = ftext.splitlines()
        changed = False
        for n in ast.walk(ftree):
            if isinstance(n, ast.ImportFrom) and n.module == from_module:
                if any(a.name == node.name for a in n.names):
                    if len(n.names) == 1:
                        flines[n.lineno - 1] = flines[n.lineno - 1].replace(from_module, to_module, 1)
                        changed = True
                    else:
                        needs_manual.append(f"{relpath}:{n.lineno} imports {node.name} among others from {from_module}")
            elif isinstance(n, ast.Import) and any(a.name == from_module for a in n.names):
                if f"{from_module.split('.')[-1]}.{node.name}" in ftext:
                    needs_manual.append(f"{relpath} uses {from_module}.{node.name} via `import {from_module}`")
        if changed:
            operations.append({"op": "write_file", "file": relpath, "new_text": "\n".join(flines) + ("\n" if ftext.endswith("\n") else "")})
            importers_updated += 1

    result = workspace.execute_transaction(
        operations, validate=validate or ["syntax", "tests"], commit=commit
    )
    result.update(
        {
            "moved": f"{symbol} : {from_file} -> {to_file}",
            "back_imports_added": back_imports,
            "importers_updated": importers_updated,
            "needs_manual": needs_manual,
            "confidence": "medium" if not needs_manual else "low",
        }
    )
    return result


def _insert_import(lines: list[str], module: str, name: str) -> list[str]:
    """Add `from module import name` after the last existing import (or the docstring)."""
    stmt = f"from {module} import {name}"
    if stmt in lines:
        return lines
    last_import = -1
    for i, line in enumerate(lines):
        if line.startswith(("import ", "from ")):
            last_import = i
    if last_import >= 0:
        return lines[: last_import + 1] + [stmt] + lines[last_import + 1 :]
    insert_at = 1 if lines and lines[0].startswith(('"""', "'''")) else 0
    return lines[:insert_at] + [stmt] + lines[insert_at:]


__all__ = ["rename_parameter", "change_signature_full", "move_symbol"]
