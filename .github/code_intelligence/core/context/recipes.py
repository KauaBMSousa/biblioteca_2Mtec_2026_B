"""Reusable local transformation recipes (ENHANCEME §T).

Common refactors encoded once, as parameters over the primitives already
built (`ast_transform`, the import tools, `unreferenced_symbols` + the
transaction engine). The agent names the recipe and its parameters; the
machine runs the mechanical steps and returns counts.

Every recipe runs through the transaction engine, so `commit=False` is a
real dry run and a failed validation rolls the whole thing back.
"""

from typing import Any

from code_intelligence.core.context.deadcode import IncompleteCoverageError


def _replace_api(workspace: Any, args: dict[str, Any], commit: bool) -> dict[str, Any]:
    """Swap every call `pattern` -> `replacement` across a scope (deprecated-API migration)."""
    return workspace.ast_transform(
        args["pattern"],
        args["replacement"],
        scope=args.get("scope"),
        language=args.get("language"),
        include_tests=args.get("include_tests", True),
        validate=args.get("validate"),
        commit=commit,
    )


def _remove_unused_imports(workspace: Any, args: dict[str, Any], commit: bool) -> dict[str, Any]:
    return workspace.remove_unused_imports(
        args.get("scope"), validate=args.get("validate"), commit=commit
    )


def _organize_imports(workspace: Any, args: dict[str, Any], commit: bool) -> dict[str, Any]:
    return workspace.organize_imports(
        args.get("scope"), validate=args.get("validate"), commit=commit
    )


def _remove_dead_code(workspace: Any, args: dict[str, Any], commit: bool) -> dict[str, Any]:
    """Delete functions/methods that the exact index shows nothing references.

    Refuses (via `unreferenced_symbols`) when semantic coverage is
    incomplete — an uncovered caller and an absent caller look identical,
    and only one is safe to delete. `commit` defaults to False upstream:
    this is a candidate list first, an edit second.
    """
    candidates = workspace.unreferenced_symbols(
        args.get("path_prefix"), args.get("language"), args.get("limit", 50)
    )
    entries = candidates.get("candidates") or candidates.get("symbols") or []
    operations = [
        {"op": "replace_symbol", "file": entry["file"], "symbol": entry["symbol_id"], "new_text": ""}
        for entry in entries
    ]
    if not operations:
        return {"outcome": "no_matches", "committed": False, "candidates": entries}
    result = workspace.execute_transaction(
        operations, validate=args.get("validate") or ["syntax", "tests"], commit=commit
    )
    result["candidates"] = entries
    return result


#: name -> (one-line description, handler)
RECIPES: dict[str, tuple[str, Any]] = {
    "replace_api": ("swap every call matching an ast-grep pattern for a replacement", _replace_api),
    "remove_unused_imports": ("drop unused imports across a scope (Python/ruff)", _remove_unused_imports),
    "organize_imports": ("sort imports + drop unused ones (Python/ruff)", _organize_imports),
    "remove_dead_code": ("delete symbols the exact index shows nothing references", _remove_dead_code),
}


def list_recipes() -> dict[str, str]:
    """Every recipe name and its one-line description."""
    return {name: description for name, (description, _handler) in RECIPES.items()}


def run_recipe(
    workspace: Any, name: str, args: dict[str, Any] | None = None, commit: bool = True
) -> dict[str, Any]:
    """Run the named recipe. `remove_dead_code` defaults to a dry run unless `commit` is set."""
    if name not in RECIPES:
        raise ValueError(f"unknown recipe {name!r}; available: {sorted(RECIPES)}")
    _description, handler = RECIPES[name]
    try:
        return handler(workspace, args or {}, commit)
    except IncompleteCoverageError as exc:
        return {"outcome": "refused", "committed": False, "reason": str(exc)}


__all__ = ["RECIPES", "list_recipes", "run_recipe"]
