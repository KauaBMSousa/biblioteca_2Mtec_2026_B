"""L0 — the one compact "state of the workspace" call (ENHANCEME §B, §Z).

`workspace_snapshot()` is meant to be the normal FIRST call of a session:
a single round trip that answers "is the index fresh, is the tree clean,
where is the semantic coverage thin, how much needs attention, and what
should I look at next" — with no source content and no per-finding detail.
Every number here is a count or a short string; the follow-up tools
(`get_agent_context`, `get_violations`, `impact`, ...) drill in.

It is pure composition over queries the workspace already answers
(`status`, `revisions`, `git_status`, `semantic_coverage`,
`get_agent_context`), so it can never disagree with them — it just spares
the agent four calls and the token cost of their full payloads.
"""

from pathlib import Path
from typing import Any

from code_intelligence.core.git import status as git_status_module
from code_intelligence.core.index import semantic_store
from code_intelligence.core.index.store import IndexStore
from code_intelligence.core.workspace.revisions import revisions


def _git_summary(root: Path) -> dict[str, Any]:
    """Branch + changed-file counts, or `{"repo": False}` outside a git repo."""
    try:
        status = git_status_module.git_status(root)
    except RuntimeError:
        return {"repo": False}
    changed = len(status["staged"]) + len(status["unstaged"]) + len(status["untracked"])
    return {
        "repo": True,
        "branch": status["branch"],
        "ahead": status["ahead"],
        "behind": status["behind"],
        "changed_files": changed,
        "clean": changed == 0,
    }


def _semantic_summary(store: IndexStore) -> dict[str, Any]:
    """Per-language `units_ok/units` ratio, and the languages that are not fully covered."""
    report = semantic_store.coverage(store)
    thin: list[str] = []
    per_language: dict[str, float] = {}
    for language, info in report["languages"].items():
        units = info["units"] or 0
        ratio = (info["units_ok"] / units) if units else 0.0
        per_language[language] = round(ratio, 3)
        if ratio < 1.0:
            thin.append(language)
    return {"by_language": per_language, "incomplete": sorted(thin)}


def workspace_snapshot(store: IndexStore, config: dict, root: Path) -> dict[str, Any]:
    """The compact L0 state payload. No source, no per-finding detail."""
    from code_intelligence.core.context.context import get_agent_context

    files = store.list_files()
    rev = revisions(store, root)
    agent = get_agent_context(store, config)
    agent_summary = agent["summary"]
    git = _git_summary(root)

    high_priority = agent_summary["total_violations"]
    if not rev["index_fresh"]:
        recommended = "workspace_index"
    elif high_priority:
        recommended = "get_agent_context"
    elif git.get("changed_files"):
        recommended = "change_summary"
    else:
        recommended = "inspect"

    return {
        "revisions": rev,
        "index": {
            "files": len(files),
            "symbols": len(store.list_all_symbols()),
            "diagnostics": len(store.list_diagnostics()),
            "last_indexed_at": store.meta_get("last_indexed_at"),
            "fresh": rev["index_fresh"],
        },
        "git": git,
        "semantic_coverage": _semantic_summary(store),
        "attention": {
            "min_severity": agent_summary["min_severity"],
            "high_priority": high_priority,
            "by_severity": agent_summary["by_severity"],
        },
        "recommended_next_action": recommended,
    }


__all__ = ["workspace_snapshot"]
