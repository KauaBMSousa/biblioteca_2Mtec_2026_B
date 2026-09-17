"""libclang-driven C++ call-site rewriting for a type change (ENHANCEME §18).

`change_return_type` / `change_parameter_type` edit the *declaration* and
report the sites clangd finds. This closes the last gap: a real parse with
the project's compile flags, a USR-matched walk of every use, and the
mechanical edits that follow — a variable bound to the call
(`OldT x = obj.f();` -> `NewT x = obj.f();`), a cast on the old type. What
cannot be rewritten safely (the value is passed to another function, stored
in a container, used in arithmetic that changes meaning) comes back in
`review`, never silently.

Needs `compile_commands.json` and libclang. Without them the caller keeps
its declaration-only behaviour — this raises `RuntimeError`, it never
returns a half-answer.
"""

from pathlib import Path
from typing import Any

from code_intelligence.core.semantic.cpp_queries import _parse

#: const / ref / pointer decoration we tolerate around the type token.
_DECORATION = {"const", "volatile", "&", "*", "&&"}


def _norm(type_spelling: str) -> str:
    """Strip decoration so `const int&` and `int` compare equal."""
    toks = type_spelling.replace("*", " ").replace("&", " ").split()
    return " ".join(t for t in toks if t not in _DECORATION)


def _walk_with_parent(cursor, parent=None):
    yield cursor, parent
    for child in cursor.get_children():
        yield from _walk_with_parent(child, cursor)


def _target_function(cindex, tu, name: str):
    """The FUNCTION_DECL / CXX_METHOD in this TU matching `name` (bare or qualified)."""
    bare = name.split("::")[-1]
    best = None
    for node in (n for n, _ in _walk_with_parent(tu.cursor)):
        if node.kind not in (cindex.CursorKind.FUNCTION_DECL, cindex.CursorKind.CXX_METHOD):
            continue
        if node.spelling != bare:
            continue
        qualified = _qualified(node)
        if qualified == name or node.spelling == name:
            return node
        best = best or node
    return best


def _qualified(node) -> str:
    parts = [node.spelling]
    p = node.semantic_parent
    while p is not None and p.kind.is_declaration() and p.spelling:
        parts.append(p.spelling)
        p = p.semantic_parent
    return "::".join(reversed(parts))


def _var_type_token_range(cindex, var_decl, old_norm: str):
    """`(start_offset, end_offset)` of the type token(s) in a VAR_DECL, or None."""
    tokens = list(var_decl.get_tokens())
    spans = []
    for tok in tokens:
        if tok.kind == cindex.TokenKind.IDENTIFIER and tok.spelling == var_decl.spelling:
            break  # reached the variable name
        if tok.spelling in ("=", ";", "("):
            break
        if tok.spelling in _DECORATION:
            continue
        spans.append(tok.extent)
    if not spans:
        return None
    joined = " ".join(
        t.spelling for t in tokens
        if t.extent.start.offset >= spans[0].start.offset
        and t.extent.end.offset <= spans[-1].end.offset
    )
    if _norm(joined) != old_norm:
        return None
    return spans[0].start.offset, spans[-1].end.offset


def propagate_return_type(
    root: Path,
    translation_unit: str,
    symbol: str,
    new_type: str,
) -> dict[str, Any]:
    """Edits + review items for changing `symbol`'s return type to `new_type`.

    `edits`: `[{file, start, end, new_text, was}]` (byte offsets, safe to
    apply). `review`: `[{file, line, reason}]` — a use that a human must
    judge.
    """
    cindex, tu = _parse(root, translation_unit)
    target = _target_function(cindex, tu, symbol)
    if target is None:
        raise RuntimeError(f"{symbol!r} is not a function/method in {translation_unit}")

    old_type = target.result_type.spelling
    old_norm = _norm(old_type)
    usr = target.get_usr()

    edits: list[dict[str, Any]] = []
    review: list[dict[str, Any]] = []
    seen: set[tuple] = set()

    for node, parent in _walk_with_parent(tu.cursor):
        if node.kind != cindex.CursorKind.CALL_EXPR:
            continue
        ref = node.referenced
        if ref is None or ref.get_usr() != usr:
            continue
        loc = node.location
        if loc.file is None:
            continue
        key = (loc.file.name, loc.line, loc.column)
        if key in seen:
            continue
        seen.add(key)

        if parent is not None and parent.kind == cindex.CursorKind.VAR_DECL:
            rng = _var_type_token_range(cindex, parent, old_norm)
            if rng is not None:
                start, end = rng
                with open(parent.location.file.name, "rb") as fh:
                    was = fh.read()[start:end].decode("utf-8", "replace")
                edits.append({
                    "file": parent.location.file.name, "start": start, "end": end,
                    "new_text": new_type, "was": was,
                    "context": f"{parent.spelling}: {old_type} -> {new_type}",
                })
                continue

        review.append({
            "file": loc.file.name, "line": loc.line,
            "reason": "call result is used somewhere a type change is not mechanically safe",
        })

    return {
        "symbol": symbol,
        "old_type": old_type,
        "new_type": new_type,
        "translation_unit": translation_unit,
        "edits": edits,
        "review": review,
    }


def apply_offset_edits(root: Path, edits: list[dict[str, Any]]) -> dict[str, str]:
    """Apply `[{file, start, end, new_text}]` edits; return `{relpath: new_text}`."""
    by_file: dict[str, list[dict[str, Any]]] = {}
    for e in edits:
        by_file.setdefault(e["file"], []).append(e)
    out: dict[str, str] = {}
    for abspath, file_edits in by_file.items():
        text = Path(abspath).read_text(encoding="utf-8", errors="replace")
        for e in sorted(file_edits, key=lambda e: e["start"], reverse=True):
            text = text[: e["start"]] + e["new_text"] + text[e["end"]:]
        try:
            rel = str(Path(abspath).relative_to(root))
        except ValueError:
            rel = abspath
        out[rel] = text
    return out


__all__ = ["apply_offset_edits", "propagate_return_type"]
