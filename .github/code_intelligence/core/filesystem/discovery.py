"""Discovers which files to analyze.

Two discovery strategies are supported:

- Full discovery: every tracked file (``git ls-files``) unioned with every
  untracked-but-not-ignored file (``git ls-files --others
  --exclude-standard``), which is what makes ``respect_gitignore`` work with
  zero extra configuration — vendored/generated trees that are already
  git-ignored are excluded automatically.
- ``--changed-only`` discovery: files touched relative to a base ref, plus
  the uncommitted working-tree diff, plus untracked-not-ignored files.

Both strategies fall back to a plain filesystem walk when the target is not
inside a git repository (except ``--changed-only``, which requires git and
errors otherwise).

Ported unchanged (besides import paths) from
`tools/code_quality/code_quality/discovery.py` — including the already-
fixed toplevel-vs-root path-relativity logic (`_pathspec_for`/
`_rebase_to_root`): git-diff paths are toplevel-relative but symbol paths
are root-relative, and that distinction must be preserved exactly.
"""

import fnmatch
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from code_intelligence.core.filesystem.discovery_error import DiscoveryError

__all__ = [
    "DiscoveryError",
    "DiscoveryResult",
    "discover_all",
    "discover_changed",
    "discover_changed_hunks",
    "is_git_repo",
    "matches_exclude_globs",
    "all_extensions",
    "is_relevant_path",
    "resolve_base_ref",
]

#: A sentinel end-line meaning "the whole file is changed" (untracked files,
#: which have no diff hunks against anything).
WHOLE_FILE_CHANGED = (1, 10**9)

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(?P<start>\d+)(?:,(?P<count>\d+))? @@")


@dataclass(slots=True)
class DiscoveryResult:
    """The outcome of a discovery pass.

    Attributes:
        files: Absolute paths of files selected for analysis, sorted.
        is_git_repo: Whether the root was inside a git repository.
        base_ref_used: The resolved base ref, when in changed-only mode.
    """

    files: list[Path]
    is_git_repo: bool
    base_ref_used: str | None = None


def _run_git(args: list[str], cwd: Path) -> str:
    """Run a git subcommand and return its stdout, raising on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise DiscoveryError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout


def is_git_repo(root: Path) -> bool:
    """Return True when ``root`` is inside a git working tree."""
    result = subprocess.run(
        ["git", "rev-parse", "--is-inside-work-tree"],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and result.stdout.strip() == "true"


def _git_toplevel(root: Path) -> Path:
    """Return the git repository's top-level directory for ``root``."""
    out = _run_git(["rev-parse", "--show-toplevel"], cwd=root)
    return Path(out.strip())


def _pathspec_for(root: Path, toplevel: Path) -> str:
    """Return the git pathspec that scopes a toplevel-run command to `root`.

    Git commands here are always run with `cwd=toplevel` so their output
    paths are consistently toplevel-relative (`git ls-files` is cwd-relative
    by default, while `git diff --name-only` is always toplevel-relative —
    running everything from `toplevel` with an explicit pathspec avoids that
    inconsistency entirely).
    """
    rel = root.resolve().relative_to(toplevel.resolve())
    return "." if str(rel) == "." else str(rel)


def _matches_extension(path: Path, extensions: set[str]) -> bool:
    """Return True when ``path``'s suffix is one of the configured extensions."""
    return path.suffix in extensions


def matches_exclude_globs(relpath: str, exclude_globs: list[str]) -> bool:
    """Return True when ``relpath`` matches any of the configured exclude globs.

    Public (not `_`-prefixed): also reused by `daemon/server.py`'s file
    watcher, so a directory the indexer would never analyze (`.venv/`,
    `__pycache__/`, `node_modules/`, ...) doesn't spuriously retrigger a
    reindex on every write inside it either.
    """
    for pattern in exclude_globs:
        if fnmatch.fnmatch(relpath, pattern) or fnmatch.fnmatch("/" + relpath, pattern):
            return True
        # Also match against any path component for patterns like **/vendor/**.
        stripped = pattern.replace("**/", "").replace("/**", "")
        if stripped and stripped in relpath.split("/"):
            return True
    return False


def all_extensions(config: dict) -> set[str]:
    """Flatten the config's per-language extension map into one set."""
    exts: set[str] = set()
    for lang_exts in config["languages"].values():
        exts.update(lang_exts)
    return exts


def is_relevant_path(relpath: str, config: dict) -> bool:
    """True when `relpath` is one the indexer would ever analyze: a configured
    language extension, and not matched by `exclude_globs`.

    Public specifically so `daemon/server.py`'s file watcher can use the
    *exact* same relevance test the indexer itself uses, instead of an
    approximate directory-name blocklist — a real repo has noise sources an
    exclude-glob list will never fully enumerate (e.g. a live application's
    own log file living inside a stowed dotfiles config directory); the one
    test that can never miss is "would the indexer even look at this file's
    extension in the first place".
    """
    if matches_exclude_globs(relpath, config.get("exclude_globs", [])):
        return False
    return Path(relpath).suffix in all_extensions(config)


def _filter_paths(
    root: Path, paths: list[str], config: dict
) -> list[Path]:
    """Apply extension and exclude-glob filtering to a list of repo-relative paths."""
    extensions = all_extensions(config)
    exclude_globs = config.get("exclude_globs", [])
    selected: list[Path] = []
    for rel in paths:
        rel = rel.strip()
        if not rel:
            continue
        candidate = Path(rel)
        if not _matches_extension(candidate, extensions):
            continue
        if matches_exclude_globs(rel, exclude_globs):
            continue
        abs_path = root / rel
        if abs_path.is_file():
            selected.append(abs_path)
    return selected


def discover_all(root: Path, config: dict) -> DiscoveryResult:
    """Discover every analyzable file under ``root``.

    When ``root`` is a git repository and ``respect_gitignore`` is enabled,
    uses ``git ls-files`` (tracked) unioned with
    ``git ls-files --others --exclude-standard`` (untracked, not ignored).
    Otherwise falls back to a plain recursive filesystem walk.
    """
    if config.get("respect_gitignore", True) and is_git_repo(root):
        toplevel = _git_toplevel(root)
        pathspec = _pathspec_for(root, toplevel)
        tracked = _run_git(["ls-files", "--", pathspec], cwd=toplevel).splitlines()
        untracked = _run_git(
            ["ls-files", "--others", "--exclude-standard", "--", pathspec], cwd=toplevel
        ).splitlines()
        all_rel = sorted(set(tracked) | set(untracked))
        files = _filter_paths(toplevel, all_rel, config)
        return DiscoveryResult(files=sorted(files), is_git_repo=True)

    extensions = all_extensions(config)
    exclude_globs = config.get("exclude_globs", [])
    files = []
    for candidate in root.rglob("*"):
        if not candidate.is_file():
            continue
        if candidate.suffix not in extensions:
            continue
        rel = str(candidate.relative_to(root))
        if matches_exclude_globs(rel, exclude_globs):
            continue
        files.append(candidate)
    return DiscoveryResult(files=sorted(files), is_git_repo=is_git_repo(root))


def resolve_base_ref(root: Path, explicit: str | None) -> str:
    """Resolve the base ref for ``--changed-only`` diffing.

    Tries, in order: the explicit ``--base-ref``, ``origin/HEAD``, ``main``,
    ``master``. Raises DiscoveryError if none resolve.
    """
    if explicit:
        return explicit

    for candidate in ("origin/HEAD", "main", "master"):
        result = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", candidate],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            return candidate

    raise DiscoveryError(
        "Could not auto-detect a base ref (tried origin/HEAD, main, master). "
        "Pass --base-ref explicitly."
    )


def _collect_changed_paths(toplevel: Path, pathspec: str, resolved_ref: str) -> set[str]:
    """Union every source of "changed" paths: base-ref diff, working tree, staged, untracked."""
    changed: set[str] = set()

    diff_range = _run_git(
        ["diff", "--name-only", "--diff-filter=ACMR", f"{resolved_ref}...HEAD", "--", pathspec],
        cwd=toplevel,
    )
    changed.update(line.strip() for line in diff_range.splitlines() if line.strip())

    working_tree = _run_git(
        ["diff", "--name-only", "--diff-filter=ACMR", "--", pathspec], cwd=toplevel
    )
    changed.update(line.strip() for line in working_tree.splitlines() if line.strip())

    staged = _run_git(
        ["diff", "--name-only", "--diff-filter=ACMR", "--cached", "--", pathspec], cwd=toplevel
    )
    changed.update(line.strip() for line in staged.splitlines() if line.strip())

    untracked = _run_git(
        ["ls-files", "--others", "--exclude-standard", "--", pathspec], cwd=toplevel
    )
    changed.update(line.strip() for line in untracked.splitlines() if line.strip())
    return changed


def discover_changed(root: Path, config: dict, base_ref: str | None) -> DiscoveryResult:
    """Discover files changed relative to a base ref, plus working-tree changes.

    "Changed" = ``git diff --name-only --diff-filter=ACMR <base>...HEAD``
    unioned with the uncommitted working-tree diff and untracked-not-ignored
    files, filtered to configured extensions.

    Raises:
        DiscoveryError: If ``root`` is not a git repository, or no base ref
            could be resolved.
    """
    if not is_git_repo(root):
        raise DiscoveryError("--changed-only requires a git repository")

    toplevel = _git_toplevel(root)
    pathspec = _pathspec_for(root, toplevel)
    resolved_ref = resolve_base_ref(root, base_ref)

    changed = _collect_changed_paths(toplevel, pathspec, resolved_ref)

    files = _filter_paths(toplevel, sorted(changed), config)
    return DiscoveryResult(files=sorted(files), is_git_repo=True, base_ref_used=resolved_ref)


def _parse_hunks_by_file(diff_text: str) -> dict[str, list[tuple[int, int]]]:
    """Parse a `git diff -U0` text blob into `{relpath: [(start_line, end_line), ...]}`.

    Only the new-file ("+") side of each hunk is kept — that's the line
    range in the file's *current* version, which is what a symbol's
    `start_line`/`end_line` is expressed in. A pure-deletion hunk (new-side
    count 0) still marks the single line position where the deletion
    occurred, so it isn't silently dropped.
    """
    hunks_by_file: dict[str, list[tuple[int, int]]] = {}
    current_file: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            raw_path = line[len("+++ ") :].strip()
            current_file = None if raw_path == "/dev/null" else raw_path.removeprefix("b/")
            continue
        if current_file is None or not line.startswith("@@"):
            continue
        match = _HUNK_HEADER.match(line)
        if not match:
            continue
        start = int(match.group("start"))
        count = int(match.group("count")) if match.group("count") is not None else 1
        span = (start, start) if count == 0 else (start, start + count - 1)
        hunks_by_file.setdefault(current_file, []).append(span)
    return hunks_by_file


def _merge_hunk_maps(*maps: dict[str, list[tuple[int, int]]]) -> dict[str, list[tuple[int, int]]]:
    """Union several `{relpath: [(start, end), ...]}` maps into one."""
    merged: dict[str, list[tuple[int, int]]] = {}
    for one_map in maps:
        for relpath, spans in one_map.items():
            merged.setdefault(relpath, []).extend(spans)
    return merged


def _rebase_to_root(toplevel_relpath: str, pathspec: str) -> str:
    """Convert a git-toplevel-relative path to a `root`-relative one.

    `git diff`/`git ls-files` output is always toplevel-relative, but
    `FileAnalysis.relpath` (and therefore every `SymbolRecord.file`) is
    relative to the analysis root, which can be a subdirectory of the
    toplevel. `pathspec` is exactly the toplevel-relative form of `root`
    itself (see `_pathspec_for`), so stripping it as a prefix recovers the
    root-relative path — `"."` means root and toplevel are the same
    directory, so no stripping is needed.
    """
    if pathspec == ".":
        return toplevel_relpath
    prefix = pathspec.rstrip("/") + "/"
    return toplevel_relpath.removeprefix(prefix)


def discover_changed_hunks(root: Path, config: dict, base_ref: str | None) -> dict[str, list[tuple[int, int]]]:
    """Return `{relpath: [(start_line, end_line), ...]}` for every changed hunk.

    `relpath` is relative to `root` (matching `FileAnalysis.relpath`'s
    convention), not to the git toplevel — see `_rebase_to_root`. Unions
    the same 3 diff-based sources `discover_changed` unions (base-ref diff,
    working-tree diff, staged diff), each parsed with `git diff -U0` so
    hunk headers give exact line ranges rather than a whole-file diff.
    Untracked files (no diff to parse against) get the
    `WHOLE_FILE_CHANGED` sentinel span — every symbol in them counts as
    changed.

    Raises:
        DiscoveryError: If `root` is not a git repository, or no base ref
            could be resolved.
    """
    if not is_git_repo(root):
        raise DiscoveryError("--changed-symbols requires a git repository")

    toplevel = _git_toplevel(root)
    pathspec = _pathspec_for(root, toplevel)
    resolved_ref = resolve_base_ref(root, base_ref)

    diff_range = _run_git(["diff", "-U0", "--diff-filter=ACMR", f"{resolved_ref}...HEAD", "--", pathspec], cwd=toplevel)
    working_tree = _run_git(["diff", "-U0", "--diff-filter=ACMR", "--", pathspec], cwd=toplevel)
    staged = _run_git(["diff", "-U0", "--diff-filter=ACMR", "--cached", "--", pathspec], cwd=toplevel)

    hunks = _merge_hunk_maps(
        _parse_hunks_by_file(diff_range), _parse_hunks_by_file(working_tree), _parse_hunks_by_file(staged)
    )

    untracked = _run_git(["ls-files", "--others", "--exclude-standard", "--", pathspec], cwd=toplevel)
    for relpath in (line.strip() for line in untracked.splitlines() if line.strip()):
        hunks.setdefault(relpath, []).append(WHOLE_FILE_CHANGED)

    return {_rebase_to_root(relpath, pathspec): spans for relpath, spans in hunks.items()}
