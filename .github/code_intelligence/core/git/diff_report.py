"""Regression-comparison logic: report-diffing (legacy) plus ref-based `diff`.

Two layers:

1. `compare(baseline_report, current_report)` — ported unchanged from
   `tools/code_quality/code_quality/diff_report.py`. Loads a previously
   saved `--json`-shaped report, compares it against the current run's
   report, and emits `{regressions, resolved, unchanged}`. Matching key is
   `(symbol_id, code)` when a violation carries a `symbol_id`, else
   `(file, code, line)`.

2. New primitives for fixme §35's `code-intelligence diff <ref>`: resolving
   a comparison point (`working-tree`/`staged`/`HEAD`/`HEAD~N`/a commit sha/
   a branch name) to the git args needed to list changed files and fetch a
   file's content *as of* that point, plus `symbol_diff`/`violation_diff`
   helpers reused by `core/workspace/workspace.py`'s `diff()` method (which
   owns the actual re-parse-old-content-and-compare orchestration, since
   that needs the adapters/rules the git layer doesn't know about).
"""

import subprocess
from pathlib import Path
from typing import Any

ViolationKey = tuple[str, ...]

#: Special (non-git-ref) comparison points fixme §35 asks for explicitly,
#: beyond a plain git ref (HEAD, HEAD~N, a commit sha, a branch name).
WORKING_TREE = "working-tree"
STAGED = "staged"


def _violation_key(violation: dict) -> ViolationKey:
    """Build the matching key for one serialized violation dict."""
    symbol_id = violation.get("symbol_id")
    if symbol_id:
        return ("symbol", symbol_id, violation["code"])
    return ("location", violation["file"], violation["code"], str(violation["line"]))


def _all_violations(report: dict) -> list[dict]:
    """Flatten every violation across a report's `files[]` (works for v1 or v2 schema)."""
    violations: list[dict] = []
    for file_entry in report.get("files", []):
        violations.extend(file_entry.get("violations", []))
    return violations


def _group_by_key(violations: list[dict]) -> dict[ViolationKey, list[dict]]:
    """Group serialized violations by their matching key (see `_violation_key`)."""
    grouped: dict[ViolationKey, list[dict]] = {}
    for violation in violations:
        grouped.setdefault(_violation_key(violation), []).append(violation)
    return grouped


def compare(baseline_report: dict, current_report: dict) -> dict[str, Any]:
    """Compare a baseline report against the current run's report.

    Returns:
        `{"regressions": [...], "resolved": [...], "unchanged": [...]}`.
        Each `regressions`/`unchanged` entry is `{"before": dict|None, "after": dict}`;
        each `resolved` entry is `{"before": dict}`.
    """
    baseline_by_key = _group_by_key(_all_violations(baseline_report))
    current_by_key = _group_by_key(_all_violations(current_report))

    regressions: list[dict] = []
    unchanged: list[dict] = []
    for key, currents in current_by_key.items():
        befores = baseline_by_key.get(key, [])
        if not befores:
            regressions.extend({"before": None, "after": violation} for violation in currents)
            continue
        for index, violation in enumerate(currents):
            before = befores[index] if index < len(befores) else befores[0]
            unchanged.append({"before": before, "after": violation})

    resolved: list[dict] = []
    for key, befores in baseline_by_key.items():
        if key not in current_by_key:
            resolved.extend({"before": violation} for violation in befores)

    return {"regressions": regressions, "resolved": resolved, "unchanged": unchanged}


def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output, never raising on a nonzero exit."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=False)


def _git_toplevel(root: Path) -> Path | None:
    """Return the git repository's top-level directory for `root`, or None outside git."""
    result = _run_git(["rev-parse", "--show-toplevel"], cwd=root)
    if result.returncode != 0:
        return None
    return Path(result.stdout.strip())


def _pathspec_for(root: Path, toplevel: Path) -> str:
    """Return the git pathspec that scopes a toplevel-run command to `root`.

    Mirrors `core/filesystem/discovery.py`'s `_pathspec_for`/`_rebase_to_root`
    fix: `git diff --name-only`/`git show` always resolve relative to the
    repo toplevel, never to `cwd` — running from `toplevel` with an
    explicit pathspec, then rebasing results back to `root`-relative, is
    what keeps `changed_files_for`'s output consistent with
    `SymbolRecord.file`'s root-relative convention when `root` is a
    subdirectory of the toplevel (the same class of bug already fixed
    there).
    """
    rel = root.resolve().relative_to(toplevel.resolve())
    return "." if str(rel) == "." else str(rel)


def _rebase_to_root(toplevel_relpath: str, pathspec: str) -> str:
    """Convert a git-toplevel-relative path to a `root`-relative one."""
    if pathspec == ".":
        return toplevel_relpath
    prefix = pathspec.rstrip("/") + "/"
    return toplevel_relpath.removeprefix(prefix)


def changed_files_for(root: Path, point: str) -> list[str]:
    """List `root`-relative files changed relative to comparison point `point`.

    `point` is `"working-tree"` (uncommitted, unstaged changes vs HEAD),
    `"staged"` (index vs HEAD), or any git-recognized ref (`"HEAD"`,
    `"HEAD~1"`, a commit sha, a branch name) — diffed against the working
    tree. Returns `[]` outside a git repository.
    """
    toplevel = _git_toplevel(root)
    if toplevel is None:
        return []
    pathspec = _pathspec_for(root, toplevel)

    if point == WORKING_TREE:
        args = ["diff", "--name-only", "--diff-filter=ACMR", "--", pathspec]
    elif point == STAGED:
        args = ["diff", "--name-only", "--diff-filter=ACMR", "--cached", "--", pathspec]
    else:
        args = ["diff", "--name-only", "--diff-filter=ACMR", point, "--", pathspec]
    result = _run_git(args, cwd=toplevel)
    if result.returncode != 0:
        return []
    return [_rebase_to_root(line.strip(), pathspec) for line in result.stdout.splitlines() if line.strip()]


def diff_stat_for(root: Path, point: str) -> dict[str, Any]:
    """Per-file insertions/deletions relative to comparison point `point` (see `changed_files_for`).

    The line-level counts `changed_files_for` doesn't carry -- "which files
    changed" answers a different question from "how much", and the second
    one is what decides whether a diff is worth reading in full versus
    skimming a summary. Binary files report `insertions`/`deletions` as `0`
    with `"binary": true` (git's `--numstat` prints `-`/`-` for those, never
    a real count).
    """
    toplevel = _git_toplevel(root)
    if toplevel is None:
        return {"files": [], "total_insertions": 0, "total_deletions": 0}
    pathspec = _pathspec_for(root, toplevel)

    if point == WORKING_TREE:
        args = ["diff", "--numstat", "--", pathspec]
    elif point == STAGED:
        args = ["diff", "--numstat", "--cached", "--", pathspec]
    else:
        args = ["diff", "--numstat", point, "--", pathspec]
    result = _run_git(args, cwd=toplevel)
    if result.returncode != 0:
        return {"files": [], "total_insertions": 0, "total_deletions": 0}

    files: list[dict[str, Any]] = []
    total_insertions = total_deletions = 0
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        added, removed, path = line.split("\t", 2)
        is_binary = added == "-" or removed == "-"
        insertions = 0 if is_binary else int(added)
        deletions = 0 if is_binary else int(removed)
        files.append(
            {
                "file": _rebase_to_root(path.strip(), pathspec),
                "insertions": insertions,
                "deletions": deletions,
                "binary": is_binary,
            }
        )
        total_insertions += insertions
        total_deletions += deletions
    return {"files": files, "total_insertions": total_insertions, "total_deletions": total_deletions}


def content_at(root: Path, point: str, relpath: str) -> str | None:
    """Return `relpath`'s (root-relative) content as of comparison point `point`.

    `None` when the file didn't exist at that point (or outside git).
    `"working-tree"` reads the file directly off disk (its current,
    possibly-uncommitted content); `"staged"` reads the index's blob
    (`git show :path`); any other `point` reads that ref's blob
    (`git show point:path`) — resolved via the same toplevel/pathspec
    rebasing `changed_files_for` uses.
    """
    if point == WORKING_TREE:
        path = root / relpath
        try:
            return path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None

    toplevel = _git_toplevel(root)
    if toplevel is None:
        return None
    pathspec = _pathspec_for(root, toplevel)
    toplevel_relpath = relpath if pathspec == "." else f"{pathspec.rstrip('/')}/{relpath}"
    ref_spec = f":{toplevel_relpath}" if point == STAGED else f"{point}:{toplevel_relpath}"
    result = _run_git(["show", ref_spec], cwd=toplevel)
    if result.returncode != 0:
        return None
    return result.stdout


def symbol_diff(old_symbol_ids: set[str], new_symbol_ids: set[str], changed_ids: set[str]) -> dict[str, list[str]]:
    """Build `{added_symbols, removed_symbols, changed_symbols}` from three symbol_id sets.

    `changed_ids` is the set of symbol_ids present in both snapshots whose
    `content_hash` differs — computed by the caller (`Workspace.diff`),
    which alone has access to both snapshots' `SymbolRecord`s.
    """
    return {
        "added_symbols": sorted(new_symbol_ids - old_symbol_ids),
        "removed_symbols": sorted(old_symbol_ids - new_symbol_ids),
        "changed_symbols": sorted(changed_ids),
    }


def violation_diff(old_violations: list[dict], new_violations: list[dict]) -> dict[str, list[dict]]:
    """Build `{new_violations, resolved_violations, regressions}` from two violation-dict lists.

    `new_violations`/`resolved_violations` are keyed the same way
    `compare()` keys a full report (`(symbol_id, code)` or
    `(file, code, line)`); `regressions` is the alias fixme §35 asks for —
    identical to `new_violations` (a violation with no counterpart in the
    "before" snapshot is, by definition, something the change introduced).
    """
    old_by_key = _group_by_key(old_violations)
    new_by_key = _group_by_key(new_violations)

    new_findings = [violation for key, group in new_by_key.items() if key not in old_by_key for violation in group]
    resolved = [violation for key, group in old_by_key.items() if key not in new_by_key for violation in group]

    return {"new_violations": new_findings, "resolved_violations": resolved, "regressions": new_findings}


__all__ = [
    "WORKING_TREE",
    "STAGED",
    "compare",
    "changed_files_for",
    "diff_stat_for",
    "content_at",
    "symbol_diff",
    "violation_diff",
]
