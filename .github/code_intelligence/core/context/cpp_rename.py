"""Type-accurate C++ rename via clangd (ENHANCEME §V, option 1).

clangd resolves the symbol under the cursor with full type information and
returns a cross-file `WorkspaceEdit`. This translates that edit into
`write_file` transaction operations, so the rename is snapshot / apply /
reindex / validate / commit-or-rollback like every other mutation, and
returns counts rather than rewritten source.

Cross-file coverage depends on clangd's background index. `rename_symbol`
waits (bounded) for the first index pass and reports `index_settled:
False` when it had to proceed early — in that case a site in a
not-yet-indexed translation unit can be missed, and the caller should
re-run once the index is warm.
"""

import re
from pathlib import Path
from typing import Any

from code_intelligence.core.context.cpp_transform import _require_cpp, _symbol_row
from code_intelligence.core.semantic.clangd_client import (
    ClangdClient,
    ClangdUnavailable,
    apply_text_edits,
    workspace_edit_to_changes,
)

__all__ = ["rename_symbol", "ClangdUnavailable"]

_IDENT = re.compile(r"[A-Za-z_]\w*")


def _declaration_position(workspace: Any, file: str, symbol: str) -> tuple[int, int, str]:
    """(0-based line, 0-based char, short_name) of the symbol's declaration identifier."""
    row = _symbol_row(workspace, file, symbol)
    if row is None:
        raise LookupError(f"{symbol!r} is not indexed in {file}")
    short = (row["name"] or symbol).split("::")[-1]
    line_text = (workspace.root / file).read_text(encoding="utf-8", errors="replace").splitlines()[
        row["start_line"] - 1
    ]
    match = re.search(rf"\b{re.escape(short)}\b", line_text)
    if match is None:
        raise LookupError(f"could not locate {short!r} on {file}:{row['start_line']}")
    return row["start_line"] - 1, match.start(), short


def rename_symbol(
    workspace: Any,
    file: str,
    symbol: str,
    new_name: str,
    *,
    dry_run: bool = True,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Rename a C++ symbol and every reference clangd resolves, across files."""
    _require_cpp(workspace, file)
    if not _IDENT.fullmatch(new_name):
        raise ValueError(f"{new_name!r} is not a valid identifier")

    line0, char0, short = _declaration_position(workspace, file, symbol)
    root: Path = workspace.root

    with ClangdClient(root) as client:
        client.did_open(file)
        settled = client.wait_for_index()
        try:
            edit = client.rename(file, line0, char0, new_name)
        except RuntimeError as exc:
            return {
                "outcome": "refused",
                "committed": False,
                "confidence": "low",
                "reason": f"clangd declined the rename: {exc}",
            }

    changes = workspace_edit_to_changes(edit or {})
    if not changes:
        return {
            "outcome": "refused",
            "committed": False,
            "confidence": "low",
            "reason": "clangd returned no edits (symbol not renameable, or nothing resolved)",
        }

    operations: list[dict[str, Any]] = []
    total_edits = 0
    for abs_path, edits in changes.items():
        try:
            relpath = str(Path(abs_path).relative_to(root))
        except ValueError:
            continue  # an edit outside the workspace — skip, report below
        current = (root / relpath).read_text(encoding="utf-8", errors="replace")
        operations.append(
            {"op": "write_file", "file": relpath, "new_text": apply_text_edits(current, edits)}
        )
        total_edits += len(edits)

    outside = [p for p in changes if not p.startswith(str(root))]
    plan = {
        "symbol": f"{short} -> {new_name}",
        "files": sorted(op["file"] for op in operations),
        "edits": total_edits,
        "index_settled": settled,
        "edits_outside_workspace": outside,
        "backend": "clangd",
    }

    if dry_run:
        return {"outcome": "planned", "committed": False, **plan}

    result = workspace.execute_transaction(
        operations, validate=validate or ["syntax"], commit=commit
    )
    result.update(plan)
    if not settled:
        result["warning"] = "clangd's index had not settled — re-run once warm to catch missed TUs"
    return result
