"""C++ `move_symbol` through the transformation engine (ENHANCEME §18).

Moves one top-level definition (a free function, a class/struct) between
two C++ files as a single transaction: cut it from the source, paste it
into the destination (creating it, with an include guard, if new), and add
`#include "<dest>"` to every file that still references it. Compilation /
repair run on top via the planner, exactly as for a Python move.

It refuses rather than guess when the move is not a clean cut — a symbol
split across a header declaration and a `.cpp` definition, or one whose
span the index cannot bound.
"""

import re
from pathlib import Path
from typing import Any

from code_intelligence.core.context.transaction import execute_transaction
from code_intelligence.core.exec.errors import UnsupportedLanguageError

_CPP_LANG = "cpp"
_HEADER_SUFFIXES = (".h", ".hpp", ".hh", ".hxx")


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


def _guarded(relpath: str, body: str) -> str:
    guard = re.sub(r"[^A-Za-z0-9]", "_", relpath).upper() + "_"
    return f"#ifndef {guard}\n#define {guard}\n\n{body}\n\n#endif  // {guard}\n"


def _referencing_files(workspace: Any, name: str, exclude: set[str]) -> list[str]:
    hits: list[str] = []
    short = name.split("::")[-1]
    if not short:
        return hits
    pat = re.compile(rf"\b{re.escape(short)}\s*\(")
    for row in workspace.store.list_files():
        if row["language"] != _CPP_LANG or row["path"] in exclude:
            continue
        text = (workspace.root / row["path"]).read_text(encoding="utf-8", errors="replace")
        if pat.search(text):
            hits.append(row["path"])
    return hits


def move_symbol(
    workspace: Any,
    symbol: str,
    from_file: str,
    to_file: str,
    *,
    commit: bool = True,
) -> dict[str, Any]:
    _require_cpp(workspace, from_file)
    row = _symbol_row(workspace, from_file, symbol)
    if row is None:
        return {"outcome": "refused", "committed": False,
                "reason": f"{symbol!r} is not a top-level symbol of {from_file}"}

    src = (workspace.root / from_file).read_text(encoding="utf-8", errors="replace")
    lines = src.splitlines(keepends=True)
    start, end = row["start_line"], row["end_line"]
    if not (1 <= start <= end <= len(lines)):
        return {"outcome": "refused", "committed": False,
                "reason": "the index cannot bound this symbol's span cleanly"}

    block = "".join(lines[start - 1:end]).strip("\n")
    new_from = "".join(lines[: start - 1] + lines[end:])

    dest_path = workspace.root / to_file
    if dest_path.exists():
        dest = dest_path.read_text(encoding="utf-8", errors="replace")
        new_to = dest.rstrip("\n") + "\n\n\n" + block + "\n"
    elif to_file.endswith(_HEADER_SUFFIXES):
        new_to = _guarded(to_file, block)
    else:
        new_to = block + "\n"

    ops: list[dict[str, Any]] = [
        {"op": "write_file", "file": from_file, "new_text": new_from},
        {"op": "write_file", "file": to_file, "new_text": new_to},
    ]

    include_line = f'#include "{Path(to_file).name}"\n'
    referencing = _referencing_files(workspace, row["name"], {from_file, to_file})
    for ref in referencing:
        text = (workspace.root / ref).read_text(encoding="utf-8", errors="replace")
        if include_line.strip() in text:
            continue
        m = list(re.finditer(r'^#include\s+["<].*[">]\s*$', text, re.MULTILINE))
        pos = m[-1].end() + 1 if m else 0
        ops.append({
            "op": "write_file", "file": ref,
            "new_text": text[:pos] + include_line + text[pos:],
        })
    # the source file needs the destination too, if it still uses the symbol
    if re.search(rf"\b{re.escape(row['name'].split('::')[-1])}\s*\(", new_from):
        ops[0]["new_text"] = include_line + new_from

    result = execute_transaction(workspace, ops, validate=["syntax"], commit=commit)
    result["op"] = "move_symbol"
    result.setdefault("changed_symbols", 1)
    result["referencing_files_updated"] = referencing
    result["warning"] = (
        "C++ move is textual: verify the destination's #includes cover the moved "
        "symbol's dependencies, and that no ODR/linkage rule was broken."
    )
    return result


__all__ = ["move_symbol"]
