"""Exact C++ structural queries via libclang, one translation unit at a time (ENHANCEME §V).

These answer the questions tree-sitter cannot: which methods override which,
which classes implement an interface, where a virtual call actually
dispatches, where a type is used, where a constructor runs. Every one needs
a real parse with the project's compile flags, so — like `clang_references`
— each takes a `translation_unit` (a `.cpp` in `compile_commands.json`) and
parses just that one. A full TU parse is seconds to a minute on a large
project; this is a targeted question, not a batch pass.

Raises `RuntimeError` (never an empty result) when libclang or the
compilation database is unavailable: "clang could not run" must not read as
"nothing found".
"""

from pathlib import Path
from typing import Any

from code_intelligence.core.semantic.cpp_clang import CppClangBackend, _load_index


def _parse(root: Path, translation_unit: str):
    """Parse one translation unit; returns `(cindex_module, TranslationUnit)`."""
    cindex = _load_index()
    backend = CppClangBackend()
    units = backend.translation_units(root)
    args = units.get(translation_unit)
    if args is None:
        raise RuntimeError(
            f"{translation_unit!r} is not in compile_commands.json — pass a .cpp the build compiles"
        )
    index = cindex.Index.create()
    try:
        tu = index.parse(str(root / translation_unit), args=args)
    except Exception as exc:  # pragma: no cover - clang crash path
        raise RuntimeError(f"libclang failed to parse {translation_unit}: {exc}") from exc
    return cindex, tu


def _walk(cursor):
    stack = [cursor]
    while stack:
        node = stack.pop()
        stack.extend(node.get_children())
        yield node


def _loc(node) -> dict[str, Any] | None:
    loc = node.location
    if loc.file is None:
        return None
    return {"file": loc.file.name, "line": loc.line, "column": loc.column}


def find_overrides(root: Path, method_name: str, translation_unit: str) -> dict[str, Any]:
    """Every method that overrides — or is overridden by — `method_name`, in this TU."""
    cindex, tu = _parse(root, translation_unit)
    results: list[dict[str, Any]] = []
    for node in _walk(tu.cursor):
        if node.kind != cindex.CursorKind.CXX_METHOD or node.spelling != method_name:
            continue
        for base in node.get_overridden_cursors() or []:
            entry = _loc(base)
            if entry:
                results.append({**entry, "relation": "overrides", "class": base.semantic_parent.spelling if base.semantic_parent else None})
    # also: methods elsewhere in the TU that override one we found
    for node in _walk(tu.cursor):
        if node.kind != cindex.CursorKind.CXX_METHOD:
            continue
        for base in node.get_overridden_cursors() or []:
            if base.spelling == method_name:
                entry = _loc(node)
                if entry:
                    results.append({**entry, "relation": "overridden_by", "class": node.semantic_parent.spelling if node.semantic_parent else None})
    return {"method": method_name, "translation_unit": translation_unit, "count": len(results), "overrides": results}


def find_implementations(root: Path, interface_name: str, translation_unit: str) -> dict[str, Any]:
    """Classes that derive from `interface_name` in this TU, and their method definitions."""
    cindex, tu = _parse(root, translation_unit)
    implementers: list[dict[str, Any]] = []
    for node in _walk(tu.cursor):
        if node.kind not in (cindex.CursorKind.CLASS_DECL, cindex.CursorKind.STRUCT_DECL):
            continue
        bases = [
            c.type.spelling
            for c in node.get_children()
            if c.kind == cindex.CursorKind.CXX_BASE_SPECIFIER
        ]
        if any(interface_name in b for b in bases):
            entry = _loc(node)
            methods = [
                c.spelling
                for c in node.get_children()
                if c.kind == cindex.CursorKind.CXX_METHOD
            ]
            if entry:
                implementers.append({**entry, "class": node.spelling, "bases": bases, "methods": methods})
    return {
        "interface": interface_name,
        "translation_unit": translation_unit,
        "count": len(implementers),
        "implementations": implementers,
    }


def find_virtual_callers(root: Path, method_name: str, translation_unit: str) -> dict[str, Any]:
    """Call sites in this TU whose callee is a virtual method named `method_name`."""
    cindex, tu = _parse(root, translation_unit)
    sites: list[dict[str, Any]] = []
    for node in _walk(tu.cursor):
        if node.kind not in (cindex.CursorKind.CALL_EXPR, cindex.CursorKind.MEMBER_REF_EXPR):
            continue
        referenced = node.referenced
        if referenced is None or referenced.spelling != method_name:
            continue
        if referenced.kind == cindex.CursorKind.CXX_METHOD and referenced.is_virtual_method():
            entry = _loc(node)
            if entry:
                sites.append({**entry, "static_type": node.type.spelling if node.type else None})
    return {"method": method_name, "translation_unit": translation_unit, "count": len(sites), "call_sites": sites}


def type_uses(root: Path, type_name: str, translation_unit: str, limit: int = 200) -> dict[str, Any]:
    """Declarations in this TU whose type mentions `type_name` (var / param / field / return)."""
    cindex, tu = _parse(root, translation_unit)
    kinds = {
        cindex.CursorKind.VAR_DECL: "variable",
        cindex.CursorKind.PARM_DECL: "parameter",
        cindex.CursorKind.FIELD_DECL: "field",
    }
    uses: list[dict[str, Any]] = []
    for node in _walk(tu.cursor):
        category = kinds.get(node.kind)
        if category and type_name in (node.type.spelling or ""):
            entry = _loc(node)
            if entry:
                uses.append({**entry, "kind": category, "name": node.spelling, "type": node.type.spelling})
        elif node.kind in (cindex.CursorKind.FUNCTION_DECL, cindex.CursorKind.CXX_METHOD):
            if type_name in (node.result_type.spelling or ""):
                entry = _loc(node)
                if entry:
                    uses.append({**entry, "kind": "return_type", "name": node.spelling, "type": node.result_type.spelling})
        if len(uses) >= limit:
            break
    return {"type": type_name, "translation_unit": translation_unit, "count": len(uses), "uses": uses}


def constructor_calls(root: Path, class_name: str, translation_unit: str) -> dict[str, Any]:
    """Where an object of `class_name` is constructed in this TU."""
    cindex, tu = _parse(root, translation_unit)
    sites: list[dict[str, Any]] = []
    for node in _walk(tu.cursor):
        if node.kind == cindex.CursorKind.CALL_EXPR:
            referenced = node.referenced
            if referenced is not None and referenced.kind == cindex.CursorKind.CONSTRUCTOR:
                if referenced.semantic_parent and referenced.semantic_parent.spelling == class_name:
                    entry = _loc(node)
                    if entry:
                        sites.append({**entry, "form": "explicit"})
        elif node.kind == cindex.CursorKind.VAR_DECL and class_name in (node.type.spelling or ""):
            entry = _loc(node)
            if entry:
                sites.append({**entry, "form": "declaration", "name": node.spelling})
    return {"class": class_name, "translation_unit": translation_unit, "count": len(sites), "sites": sites}


def macro_expansion_context(root: Path, file: str, line: int, translation_unit: str) -> dict[str, Any]:
    """The macro expanded at `file`:`line` in this TU, and its `#define`."""
    cindex, tu = _parse(root, translation_unit)
    target = file.rsplit("/", 1)[-1]
    for node in _walk(tu.cursor):
        if node.kind != cindex.CursorKind.MACRO_INSTANTIATION:
            continue
        loc = node.location
        if loc.file is None or not loc.file.name.endswith(target) or loc.line != line:
            continue
        definition = node.referenced
        define_loc = _loc(definition) if definition else None
        return {
            "file": file,
            "line": line,
            "macro": node.spelling,
            "defined_at": define_loc,
            "translation_unit": translation_unit,
        }
    return {
        "file": file,
        "line": line,
        "macro": None,
        "note": "no macro instantiation recorded at that location in this TU",
        "translation_unit": translation_unit,
    }


__all__ = [
    "find_overrides",
    "find_implementations",
    "find_virtual_callers",
    "type_uses",
    "constructor_calls",
    "macro_expansion_context",
]
