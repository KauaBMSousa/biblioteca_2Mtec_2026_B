"""C++ declaration edits (ENHANCEME §V, §18), syntactic + index-informed.

`forward_declare` inserts a `class Foo;` (optionally dropping a now-unneeded
include). The `change_*_type` family edits a declaration's type tokens; by
default it *reports* the call sites that may need attention. With
`rewrite_call_sites=True` and a `compile_commands.json`, `change_return_type`
additionally runs a libclang pass (`core.semantic.cpp_rewrite`) that folds
the mechanical call-site edits — a variable bound to the call retyped to
match — into the same transaction, and returns everything it could not
safely rewrite in `review`. Each edit runs through the transaction engine;
the impact report rides along in the result.
"""

import re
from pathlib import Path
from typing import Any

from code_intelligence.core.exec.errors import UnsupportedLanguageError
from code_intelligence.core.index import semantic_store

_CPP_LANG = "cpp"
_LEADING_QUALIFIERS = ("virtual", "static", "inline", "constexpr", "explicit", "friend")
_INCLUDE_RE = re.compile(r'^\s*#\s*include\b')


def _require_cpp(workspace: Any, file: str) -> dict[str, Any]:
    row = workspace.store.get_file(file)
    if row is None or row["language"] != _CPP_LANG:
        raise UnsupportedLanguageError(f"C++ files only (got {file!r})")
    return row


def _symbol_row(workspace: Any, file: str, symbol: str) -> dict[str, Any] | None:
    for row in workspace.store.list_symbols_for_file(file):
        if symbol in (row["symbol_id"], row["qualified_name"], row["name"]):
            return row
    return None


def _impact(workspace: Any, file: str, row: dict[str, Any]) -> dict[str, Any]:
    """Call sites that may need attention after a type change — clangd when available."""
    short = (row["name"] or "").split("::")[-1]
    line_text = (workspace.root / file).read_text(encoding="utf-8", errors="replace").splitlines()[
        row["start_line"] - 1
    ]
    match = re.search(rf"\b{re.escape(short)}\b", line_text) if short else None
    if match is not None:
        try:
            from code_intelligence.core.semantic.clangd_client import (
                ClangdClient,
                ClangdUnavailable,
            )

            with ClangdClient(workspace.root) as client:
                client.did_open(file)
                client.wait_for_index()
                locations = client.references(
                    file, row["start_line"] - 1, match.start(), include_declaration=False
                )
            sites = [
                {"file": _rel(workspace, loc["uri"]), "line": loc["range"]["start"]["line"] + 1}
                for loc in locations
            ]
            return {
                "backend": "clangd",
                "call_sites": sites[:50],
                "call_site_count": len(sites),
                "note": "type propagation to call sites is NOT automatic — review these",
            }
        except (ClangdUnavailable, RuntimeError, TimeoutError, OSError):
            pass

    edges = semantic_store.references_to(workspace.store, file, row["start_line"])
    return {
        "backend": "index",
        "call_sites": [{"file": e["from_file"], "line": e["from_line"]} for e in edges[:50]],
        "call_site_count": len(edges),
        "note": "type propagation to call sites is NOT automatic — review these",
    }


def _rel(workspace: Any, uri: str) -> str:
    path = uri.removeprefix("file://")
    try:
        return str(Path(path).relative_to(workspace.root))
    except ValueError:
        return path


def forward_declare(
    workspace: Any,
    file: str,
    symbol: str,
    kind: str = "class",
    namespace: str | None = None,
    remove_include: str | None = None,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Add `class <symbol>;` to a C++ file (optionally dropping an include it replaces)."""
    _require_cpp(workspace, file)
    if not re.fullmatch(r"[A-Za-z_]\w*", symbol):
        return {"outcome": "refused", "committed": False, "reason": f"{symbol!r} is not a simple type name"}
    if kind not in ("class", "struct"):
        raise ValueError("kind must be 'class' or 'struct'")

    text = (workspace.root / file).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    decl = f"{kind} {symbol};"
    if namespace:
        decl = f"namespace {namespace} {{ {decl} }}"
    if decl in text or f"{kind} {symbol};" in text:
        return {"outcome": "no_change", "committed": False, "rolled_back": False}

    last_include = max((i for i, line in enumerate(lines) if _INCLUDE_RE.match(line)), default=-1)
    at = last_include + 1
    while at < len(lines) and not lines[at].strip():
        at += 1
    lines.insert(at, "")
    lines.insert(at + 1, decl)

    if remove_include:
        target = remove_include.strip().strip('<>"')
        lines = [
            line for line in lines
            if not (_INCLUDE_RE.match(line) and target in line)
        ]

    new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    result = workspace.execute_transaction(
        [{"op": "write_file", "file": file, "new_text": new_text}],
        validate=validate or ["syntax"],
        commit=commit,
    )
    result["forward_declared"] = symbol
    return result


def _edit_signature(
    workspace: Any, file: str, symbol: str, rewrite, validate, commit, extra,
    extra_ops: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    row = _symbol_row(workspace, file, symbol)
    if row is None:
        return {"outcome": "refused", "committed": False, "reason": f"{symbol!r} not indexed in {file}"}
    text = (workspace.root / file).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    sig_start = row["start_line"] - 1
    sig_end = (row["body_start_line"] or row["start_line"]) - 1
    sig_end = max(sig_end, sig_start)
    signature = "\n".join(lines[sig_start : sig_end + 1])

    new_signature = rewrite(signature)
    if new_signature is None:
        return {"outcome": "refused", "committed": False, "reason": "could not locate the type token to change"}
    if new_signature == signature:
        return {"outcome": "no_change", "committed": False, "rolled_back": False}

    updated = lines[:sig_start] + new_signature.splitlines() + lines[sig_end + 1 :]
    new_text = "\n".join(updated) + ("\n" if text.endswith("\n") else "")
    ops = [{"op": "write_file", "file": file, "new_text": new_text}]
    same_file = [op for op in (extra_ops or []) if op["file"] == file]
    ops.extend(op for op in (extra_ops or []) if op["file"] != file)
    result = workspace.execute_transaction(
        ops, validate=validate or ["syntax"], commit=commit,
    )
    result["impact"] = _impact(workspace, file, row)
    result.update(extra)
    if same_file and isinstance(result.get("call_site_rewrite"), dict):
        result["call_site_rewrite"]["review"].append({
            "file": file,
            "reason": "a call site in the declaration's own file was left for manual review "
                      "(cannot combine offset and line edits in one file safely)",
        })
    return result


def _propagate(
    workspace: Any, file: str, symbol: str, new_type: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """libclang call-site propagation for a return-type change. `([ops], [review], note)`.

    Every translation unit in the compile database is parsed — the callers
    of a function declared in a header live in other `.cpp` files.
    """
    from code_intelligence.core.semantic.cpp_clang import (
        CppClangBackend,
        find_compilation_database,
    )

    if find_compilation_database(workspace.root) is None:
        return [], [], "no compile_commands.json — call sites reported only, not rewritten"
    try:
        from code_intelligence.core.semantic.cpp_rewrite import (
            apply_offset_edits,
            propagate_return_type,
        )

        units = list(CppClangBackend().translation_units(workspace.root))
    except RuntimeError as exc:
        return [], [], f"call-site propagation skipped: {exc}"
    if not units:
        return [], [], "the compile database lists no translation units"

    all_edits: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    parsed_any = False
    for tu in units:
        try:
            report = propagate_return_type(workspace.root, tu, symbol, new_type)
        except RuntimeError:
            continue  # this TU does not see the symbol; another will
        parsed_any = True
        all_edits.extend(report["edits"])
        review.extend(report["review"])
    if not parsed_any:
        return [], [], f"no translation unit references {symbol!r}"

    # de-duplicate edits and review items seen from multiple TUs
    unique_edits = {(e["file"], e["start"], e["end"]): e for e in all_edits}
    by_file = apply_offset_edits(workspace.root, list(unique_edits.values()))
    ops = [{"op": "write_file", "file": rel, "new_text": txt} for rel, txt in by_file.items()]
    unique_review = {(r["file"], r["line"]): r for r in review}
    return ops, list(unique_review.values()), None


def change_return_type(
    workspace: Any,
    file: str,
    symbol: str,
    new_type: str,
    *,
    rewrite_call_sites: bool = False,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Replace a C++ function/method's return type.

    By default reports call sites. With `rewrite_call_sites=True` and a
    `compile_commands.json`, also folds the mechanically-safe call-site
    edits into the same transaction (via libclang) and returns the rest in
    `review`.
    """
    _require_cpp(workspace, file)
    name = symbol.split("::")[-1]

    def rewrite(signature: str) -> str | None:
        idx = signature.find(name + "(")
        if idx < 0:
            idx = signature.find(name + " (")
        if idx < 0:
            return None
        head = signature[:idx]
        tokens = head.split()
        keep = [t for t in tokens if t in _LEADING_QUALIFIERS]
        scope = ""
        if tokens and "::" in tokens[-1]:
            scope = tokens[-1]
        prefix = (" ".join(keep) + " ") if keep else ""
        return f"{prefix}{new_type} {scope}{signature[idx:]}"

    extra_ops: list[dict[str, Any]] = []
    extra: dict[str, Any] = {"new_return_type": new_type}
    if rewrite_call_sites:
        extra_ops, review, note = _propagate(workspace, file, symbol, new_type)
        extra["call_site_rewrite"] = {
            "applied": len(extra_ops),
            "review": review,
            "note": note,
        }

    return _edit_signature(
        workspace, file, symbol, rewrite, validate, commit, extra, extra_ops,
    )


def change_parameter_type(
    workspace: Any,
    file: str,
    symbol: str,
    parameter: str,
    new_type: str,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Replace the type of one parameter (by name) in a C++ signature; reports call sites."""
    _require_cpp(workspace, file)

    def rewrite(signature: str) -> str | None:
        open_paren = signature.find("(")
        close_paren = signature.rfind(")")
        if open_paren < 0 or close_paren < 0:
            return None
        params = signature[open_paren + 1 : close_paren].split(",")
        changed = False
        for i, param in enumerate(params):
            if re.search(rf"\b{re.escape(parameter)}\b\s*(=|$)", param.strip()):
                params[i] = f" {new_type} {parameter}"
                changed = True
        if not changed:
            return None
        return signature[: open_paren + 1] + ",".join(params) + signature[close_paren:]

    return _edit_signature(
        workspace, file, symbol, rewrite, validate, commit,
        {"parameter": parameter, "new_type": new_type},
    )


def change_type(
    workspace: Any,
    file: str,
    line: int,
    old_type: str,
    new_type: str,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Replace `old_type` with `new_type` on one line (a local declaration)."""
    _require_cpp(workspace, file)
    text = (workspace.root / file).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    if not 1 <= line <= len(lines):
        raise ValueError(f"line {line} is outside {file}")
    current = lines[line - 1]
    replaced = re.sub(rf"\b{re.escape(old_type)}\b", new_type, current, count=1)
    if replaced == current:
        return {"outcome": "no_change", "committed": False, "rolled_back": False}
    lines[line - 1] = replaced
    new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")
    result = workspace.execute_transaction(
        [{"op": "write_file", "file": file, "new_text": new_text}],
        validate=validate or ["syntax"],
        commit=commit,
    )
    result.update({"line": line, "from_type": old_type, "to_type": new_type})
    return result


__all__ = [
    "change_parameter_type",
    "change_return_type",
    "change_type",
    "forward_declare",
]
