"""Unified, intent-level code transformation (ENHANCEME §E, §R, §U).

Two entry points, both built on the Phase 2 transaction engine so every
transformation is snapshot / apply / reindex / validate / commit-or-roll-back:

* `ast_transform` — the multi-file version of `ast_replace`. Give it a
  pattern, a replacement and a scope; it discovers every matching file,
  rewrites them as one transaction, and returns *counts* — never the
  rewritten source of 176 call sites.

* `transform` — the small operation DSL from §R: `select` a target once
  (a symbol, a file, or a scope), then apply an ordered list of `steps`.
  Each step is dispatched to the highest-level primitive that can express
  it (§U's hierarchy): a `rename` goes through the semantic
  `rename_symbol` (which refuses without exact coverage rather than
  guess), an `ast` step goes through ast-grep, a `replace` swaps a whole
  symbol definition. The agent states what it wants; this picks the engine.
"""

from typing import Any

from code_intelligence.core.context import structural
from code_intelligence.core.context.transaction import execute_transaction

#: A scoped `ast` transform touching more files than this refuses up front
#: — a rewrite that broad should be narrowed or run in reviewed batches.
_MAX_TRANSFORM_FILES = 200


def ast_transform(
    workspace: Any,
    pattern: str,
    replacement: str,
    *,
    scope: str | None = None,
    language: str | None = None,
    include_tests: bool = True,
    max_files: int | None = None,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Rewrite every match of an ast-grep pattern across a scope, atomically.

    `scope` is a path prefix (`"src/"`) or None for the whole workspace.
    Returns `{outcome, matched, files_scanned, changed_files,
    parse_failures, validation, sample}` — `sample` is a capped list of
    `{file, match_count}`, counts only, no source.
    """
    discovery = structural.ast_match_files(
        workspace.store, workspace.root, pattern, language, scope, include_tests, max_files
    )
    if discovery["over_max_files"] or discovery["files_with_matches"] > _MAX_TRANSFORM_FILES:
        return {
            "outcome": "too_broad",
            "committed": False,
            "rolled_back": False,
            "matched": discovery["total_matches"],
            "files_with_matches": discovery["files_with_matches"],
            "limit": min(max_files or _MAX_TRANSFORM_FILES, _MAX_TRANSFORM_FILES),
        }
    if not discovery["files"]:
        return {
            "outcome": "no_matches",
            "committed": False,
            "rolled_back": False,
            "matched": 0,
            "files_scanned": discovery["files_scanned"],
            "parse_failures": discovery["parse_failures"],
        }

    operations = [
        {
            "op": "ast_replace",
            "file": entry["file"],
            "pattern": pattern,
            "replacement": replacement,
            "language": entry["language"],
        }
        for entry in discovery["files"]
    ]
    result = execute_transaction(
        workspace, operations, validate=validate or ["syntax"], commit=commit
    )
    result["matched"] = discovery["total_matches"]
    result["files_scanned"] = discovery["files_scanned"]
    result["parse_failures"] = discovery["parse_failures"]
    result["sample"] = discovery["files"][:25]
    return result


def _resolve_select(workspace: Any, select: dict[str, Any]) -> dict[str, Any]:
    """Turn a `select` block into `{mode, file?, symbol?, scope?, language?}`."""
    if "symbol" in select:
        row = workspace.find_symbol(select["symbol"], select.get("file"))
        if row is None:
            raise LookupError(f"no symbol {select['symbol']!r} in the index")
        return {"mode": "symbol", "symbol": select["symbol"], "file": row["file"], "row": row}
    if "file" in select:
        return {"mode": "file", "file": select["file"]}
    if "scope" in select:
        return {"mode": "scope", "scope": select["scope"], "language": select.get("language")}
    raise ValueError("select must have one of: 'symbol', 'file', 'scope'")


def _step_to_operations(target: dict[str, Any], step: dict[str, Any]) -> list[dict[str, Any]]:
    """Lower one DSL step to transaction operations, honoring §U's hierarchy."""
    if "rename" in step:
        if target["mode"] != "symbol":
            raise ValueError("a 'rename' step requires select.symbol")
        return [
            {
                "op": "rename_symbol",
                "file": target["file"],
                "symbol": target["symbol"],
                "new_name": step["rename"],
            }
        ]
    if "replace" in step:
        if target["mode"] != "symbol":
            raise ValueError("a 'replace' step requires select.symbol")
        return [
            {
                "op": "replace_symbol",
                "file": target["file"],
                "symbol": target["symbol"],
                "new_text": step["replace"],
            }
        ]
    if "insert_before" in step:
        spec = step["insert_before"]
        file = target.get("file")
        if file is None:
            raise ValueError("an 'insert_before' step requires select.symbol or select.file")
        return [
            {"op": "insert_lines", "file": file, "line": spec["line"], "text": spec["text"]}
        ]
    if "ast" in step:
        spec = step["ast"]
        # scope-mode `ast` is handled by `transform` before this is called.
        return [
            {
                "op": "ast_replace",
                "file": target["file"],
                "pattern": spec["pattern"],
                "replacement": spec["replacement"],
                "language": spec.get("language"),
            }
        ]
    raise ValueError(f"unknown transform step: {sorted(step)}")


def transform(
    workspace: Any,
    select: dict[str, Any],
    steps: list[dict[str, Any]],
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Resolve `select` once, then apply `steps` as one transaction.

    `select`: `{"symbol": "Foo.bar"}` | `{"file": "src/foo.py"}` |
    `{"scope": "src/", "language": "python"}`.

    `steps`: an ordered list, each one of `{"rename": "newName"}`,
    `{"replace": "<full new definition>"}`,
    `{"insert_before": {"line": N, "text": "..."}}`, or
    `{"ast": {"pattern": ..., "replacement": ...}}`. `rename`/`replace`
    need `select.symbol`; a scoped `ast` step fans out across the scope.

    Returns the transaction result (`outcome`, counts, `validation`) plus
    a `resolved` block naming what `select` matched. No source is returned.

    `select={"intent": "rename Foo.bar to baz"}` (optionally with `scope`)
    skips `select`/`steps` entirely: the intent is parsed, resolved to an
    operation and run through `task()` — the engine picks the primitive.
    """
    if "intent" in select:
        from code_intelligence.core.context import planner

        return planner.task(
            workspace,
            select["intent"],
            select.get("scope"),
            commit=commit,
            repair={"enabled": True},
        )

    target = _resolve_select(workspace, select)

    if target["mode"] == "scope":
        if len(steps) != 1 or "ast" not in steps[0]:
            raise ValueError("select.scope supports exactly one 'ast' step")
        spec = steps[0]["ast"]
        result = ast_transform(
            workspace,
            spec["pattern"],
            spec["replacement"],
            scope=target["scope"],
            language=target.get("language"),
            validate=validate,
            commit=commit,
        )
        result["resolved"] = {"mode": "scope", "scope": target["scope"]}
        return result

    operations: list[dict[str, Any]] = []
    for step in steps:
        operations.extend(_step_to_operations(target, step))

    result = execute_transaction(
        workspace, operations, validate=validate or ["syntax"], commit=commit
    )
    result["resolved"] = {
        "mode": target["mode"],
        "file": target.get("file"),
        "symbol": target.get("symbol"),
    }
    return result


__all__ = ["ast_transform", "transform"]
