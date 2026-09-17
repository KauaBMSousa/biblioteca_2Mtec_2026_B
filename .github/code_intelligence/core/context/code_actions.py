"""Machine-discovered transformations for a location (ENHANCEME §S).

`code_actions(file, line)` looks at the diagnostics and the enclosing
symbol at one point and returns the transformations that apply there —
each one *self-describing*: an `id`, a human `title`, the `tool` to call
and the `arguments` to call it with (placeholders like `<NEW_NAME>` left
for the agent to fill). The model picks; the machine already knows the
shape of the call.

`apply_code_action(file, line, action_id, params)` re-derives the list,
finds `action_id`, merges `params` into its arguments, and dispatches —
stateless, so nothing has to be remembered between the two calls.
"""

from typing import Any

_PLACEHOLDER_KEYS = {"new_name", "new_symbol"}


def _enclosing_symbol(store: Any, file: str, line: int) -> dict[str, Any] | None:
    best = None
    for row in store.list_symbols_for_file(file):
        start, end = row["start_line"], row["end_line"]
        if start is None or end is None or not (start <= line <= end):
            continue
        if best is None or row["start_line"] >= best["start_line"]:
            best = row
    return best


def code_actions(workspace: Any, file: str, line: int) -> dict[str, Any]:
    """Every transformation that applies at `file`:`line`, each with a ready tool call."""
    store = workspace.store
    file_row = store.get_file(file)
    if file_row is None:
        return {"file": file, "line": line, "actions": [], "note": "file not indexed"}

    symbol = _enclosing_symbol(store, file, line)
    diagnostics = [
        d
        for d in store.list_diagnostics(file_path=file)
        if d["start_line"] <= line <= d["end_line"]
    ]
    actions: list[dict[str, Any]] = []

    if symbol is not None and symbol["kind"] in ("function", "method", "class", "struct"):
        actions.append(
            {
                "id": "rename-symbol",
                "title": f"Rename {symbol['name']!r}",
                "kind": "refactor.rename",
                "tool": "transform",
                "arguments": {
                    "select": {"symbol": symbol["qualified_name"] or symbol["name"], "file": file},
                    "steps": [{"rename": "<NEW_NAME>"}],
                },
            }
        )

    for diag in diagnostics:
        rule = diag["rule"]
        if rule == "NAMING_VIOLATION" and symbol is not None:
            continue  # already covered by the generic rename action
        if rule in ("FUNCTION_LENGTH_HIGH", "FUNCTION_LENGTH_CRITICAL", "PROCEDURAL_MONOLITH") and symbol:
            actions.append(
                {
                    "id": "extract-function",
                    "title": f"Extract part of {symbol['name']!r} into a new function",
                    "kind": "refactor.extract",
                    "tool": "extract_function",
                    "arguments": {
                        "file": file,
                        "start_line": "<START_LINE>",
                        "end_line": "<END_LINE>",
                        "new_name": "<NEW_NAME>",
                    },
                    "note": "choose a contiguous run of statements inside the function",
                }
            )
        if rule == "DUPLICATE_BLOCK":
            actions.append(
                {
                    "id": "extract-duplicate",
                    "title": "Extract the duplicated block into a shared function",
                    "kind": "refactor.extract",
                    "tool": "extract_function",
                    "arguments": {
                        "file": file,
                        "start_line": diag["start_line"],
                        "end_line": diag["end_line"],
                        "new_name": "<NEW_NAME>",
                    },
                }
            )

    if file_row["language"] == "python":
        actions.append(
            {
                "id": "organize-imports",
                "title": "Sort imports and remove unused ones in this file",
                "kind": "source.organizeImports",
                "tool": "organize_imports",
                "arguments": {"scope": file},
            }
        )

    return {
        "file": file,
        "line": line,
        "symbol": (symbol["qualified_name"] or symbol["name"]) if symbol else None,
        "diagnostics": [d["rule"] for d in diagnostics],
        "actions": actions,
    }


def apply_code_action(
    workspace: Any,
    file: str,
    line: int,
    action_id: str,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Re-derive the actions at `file`:`line`, fill `action_id`'s placeholders, dispatch."""
    params = params or {}
    available = code_actions(workspace, file, line)["actions"]
    action = next((a for a in available if a["id"] == action_id), None)
    if action is None:
        raise LookupError(
            f"no code action {action_id!r} at {file}:{line} "
            f"(available: {[a['id'] for a in available]})"
        )

    arguments = _fill(action["arguments"], params)
    unresolved = _unresolved(arguments)
    if unresolved:
        raise ValueError(
            f"code action {action_id!r} still has unfilled placeholders {unresolved}; "
            f"pass them in params"
        )

    tool = action["tool"]
    if tool == "transform":
        return workspace.transform(arguments["select"], arguments["steps"])
    if tool == "extract_function":
        return workspace.extract_function(
            arguments["file"],
            int(arguments["start_line"]),
            int(arguments["end_line"]),
            arguments["new_name"],
        )
    if tool == "organize_imports":
        return workspace.organize_imports(arguments.get("scope"))
    raise ValueError(f"code action {action_id!r} names an unknown tool {tool!r}")


def _fill(value: Any, params: dict[str, Any]) -> Any:
    """Replace `<PLACEHOLDER>` strings with `params` values (by the snake_case key)."""
    if isinstance(value, dict):
        return {key: _fill(item, params) for key, item in value.items()}
    if isinstance(value, list):
        return [_fill(item, params) for item in value]
    if isinstance(value, str) and value.startswith("<") and value.endswith(">"):
        key = value[1:-1].lower()
        return params.get(key, value)
    return value


def _unresolved(value: Any) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            found.extend(_unresolved(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_unresolved(item))
    elif isinstance(value, str) and value.startswith("<") and value.endswith(">"):
        found.append(value)
    return found


__all__ = ["code_actions", "apply_code_action"]
