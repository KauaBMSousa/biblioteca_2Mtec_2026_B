"""Hash-gated, scope-validated localized patch application.

Wraps `git apply` (inside a git repo) or GNU `patch -p1` (otherwise) — not a
custom diff/patch engine, per the plan's explicit instruction to reuse
rather than duplicate. Every apply is a hard-gated pipeline, each gate run
before the file is ever touched:

1. Locate `--symbol`'s current span via the **index** (a fast lookup, no
   reparse) — or use the whole file when `symbol` is omitted.
2. Compare that span's `content_hash`, computed fresh from the file's
   *current on-disk bytes* (never the index's possibly-stale stored hash —
   the index only locates the span; the safety gate always re-reads disk),
   against `expected_hash`. A mismatch means the file changed since the
   caller last read it — reject with `STALE_CONTEXT`, touching nothing.
3. Parse the diff's own `---`/`+++` file headers and confirm they name
   exactly the target file — reject a diff that touches anything else with
   `SCOPE_VIOLATION`, *before* ever invoking `git apply`/`patch`.
4. Dry-run the patch (`git apply --check` / `patch --dry-run`); a failure
   is `PATCH_FAILS`.
5. Apply for real only if the dry run succeeded.

This module does not re-index after a successful apply — the caller (CLI/
agent) is expected to call `Workspace.index()` again to pick up the change.

Ported from `tools/code_quality/code_quality/patch_cli.py`, with step 1
now index-backed instead of a fresh single-file reparse.
"""

import re
import subprocess
from pathlib import Path

from code_intelligence.core.filesystem.discovery import is_git_repo
from code_intelligence.core.hashes.content_hash import content_hash
from code_intelligence.core.index.store import IndexStore
from code_intelligence.core.workspace.security import PathTraversalError, resolve_within_workspace

EXIT_OK = 0
EXIT_USAGE_ERROR = 3
EXIT_STALE_CONTEXT = 10
EXIT_PATCH_FAILS = 11
EXIT_SCOPE_VIOLATION = 12

_PLUS_HEADER = re.compile(r"^\+\+\+ (?:b/)?(?P<path>.+?)(?:\t.*)?$", re.MULTILINE)
_MINUS_HEADER = re.compile(r"^--- (?:a/)?(?P<path>.+?)(?:\t.*)?$", re.MULTILINE)


class PatchResult:
    """The outcome of one `apply_patch` call."""

    def __init__(self, exit_code: int, message: str) -> None:
        """Bind a resolved exit code + human-readable message."""
        self.exit_code = exit_code
        self.message = message

    @property
    def ok(self) -> bool:
        """True when the patch was actually applied."""
        return self.exit_code == EXIT_OK

    def __repr__(self) -> str:  # pragma: no cover - debug convenience only
        return f"PatchResult(exit_code={self.exit_code}, message={self.message!r})"


def _locate_span(store: IndexStore, file_relpath: str, symbol_name: str | None) -> tuple[int, int] | None:
    """Return `(start_line, end_line)` for `symbol_name` in `file_relpath`, via the index.

    Matches by symbol_id, qualified_name, or bare name. Returns `None` both
    when `symbol_name` is `None` (whole-file case) and when no match is
    found in the index for that file (callers must distinguish those with
    an explicit "was a symbol requested" check).
    """
    if symbol_name is None:
        return None
    row = store.get_symbol(symbol_name)
    if row is not None and row["file_path"] == file_relpath:
        return row["start_line"], row["end_line"]
    for candidate in store.find_symbols_by_name(symbol_name):
        if candidate["file_path"] == file_relpath:
            return candidate["start_line"], candidate["end_line"]
    return None


def _current_hash(file_path: Path, span: tuple[int, int] | None) -> str:
    """Compute the content_hash of `file_path`'s current text, or just `span` of it."""
    text = file_path.read_text(encoding="utf-8", errors="replace")
    if span is None:
        return content_hash(text)
    start, end = span
    lines = text.splitlines()
    return content_hash("\n".join(lines[start - 1 : end]))


def _diff_touched_paths(diff_text: str) -> set[str]:
    """Return every non-`/dev/null` file path named in the diff's `---`/`+++` headers."""
    paths: set[str] = set()
    for pattern in (_PLUS_HEADER, _MINUS_HEADER):
        for match in pattern.finditer(diff_text):
            path = match.group("path").strip()
            if path != "/dev/null":
                paths.add(path)
    return paths


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess:
    """Run a subprocess, capturing output, never raising on a nonzero exit."""
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)


def apply_patch(
    root: Path,
    store: IndexStore,
    file_arg: str,
    symbol: str | None,
    expected_hash: str,
    patch_path: Path,
    dry_run: bool = False,
) -> PatchResult:
    """Run the full hash-gated, scope-validated patch pipeline.

    Args:
        root: The workspace root the diff's file paths are relative to.
        store: An open `IndexStore` for `root`, used only to *locate* the
            symbol's span (never for the safety hash comparison itself).
        file_arg: Repo-relative path of the file to patch.
        symbol: Optional symbol_id/qualified_name/bare name to scope the
            hash check to; `None` hash-checks the whole file.
        expected_hash: The `content_hash` the target span must currently
            match.
        patch_path: Path to a unified diff to apply.
        dry_run: When True, runs every gate (hash/scope/`--check`) but never
            performs the real apply — the disk is never written. Used by the
            MCP `validate_patch` tool, which must never have a mutating side
            effect hidden inside a "validate"-named call.
    """
    try:
        file_path = resolve_within_workspace(root, file_arg)
    except PathTraversalError as exc:
        return PatchResult(EXIT_USAGE_ERROR, f"error: {exc}")
    if not file_path.is_file():
        return PatchResult(EXIT_USAGE_ERROR, f"error: file does not exist: {file_path}")
    if not patch_path.is_file():
        return PatchResult(EXIT_USAGE_ERROR, f"error: patch file does not exist: {patch_path}")

    span = _locate_span(store, file_arg, symbol)
    if symbol is not None and span is None:
        return PatchResult(EXIT_USAGE_ERROR, f"error: symbol not found in {file_arg}: {symbol}")

    actual_hash = _current_hash(file_path, span)
    if actual_hash != expected_hash:
        return PatchResult(
            EXIT_STALE_CONTEXT,
            f"STALE_CONTEXT: expected {expected_hash}, but {file_arg}"
            f"{f' symbol {symbol}' if symbol else ''} currently hashes to {actual_hash} — "
            "re-read before patching; nothing was written.",
        )

    diff_text = patch_path.read_text(encoding="utf-8", errors="replace")
    touched = _diff_touched_paths(diff_text)
    out_of_scope = touched - {file_arg}
    if out_of_scope:
        return PatchResult(
            EXIT_SCOPE_VIOLATION,
            f"SCOPE_VIOLATION: patch touches {sorted(out_of_scope)}, expected only {file_arg!r} — "
            "nothing was written.",
        )

    use_git = is_git_repo(root)
    dry_run_check = (
        _run(["git", "apply", "--check", str(patch_path)], cwd=root)
        if use_git
        else _run(["patch", "--dry-run", "-p1", "-i", str(patch_path)], cwd=root)
    )
    if dry_run_check.returncode != 0:
        return PatchResult(
            EXIT_PATCH_FAILS, f"PATCH_FAILS: dry-run rejected the patch:\n{dry_run_check.stderr or dry_run_check.stdout}"
        )

    if dry_run:
        return PatchResult(EXIT_OK, f"OK (dry-run): {patch_path} would apply cleanly to {file_arg} — nothing was written.")

    result = (
        _run(["git", "apply", str(patch_path)], cwd=root)
        if use_git
        else _run(["patch", "-p1", "-i", str(patch_path)], cwd=root)
    )
    if result.returncode != 0:
        return PatchResult(EXIT_PATCH_FAILS, f"PATCH_FAILS: apply failed after a successful dry-run:\n{result.stderr or result.stdout}")

    return PatchResult(EXIT_OK, f"OK: applied {patch_path} to {file_arg}")


__all__ = [
    "apply_patch",
    "PatchResult",
    "EXIT_OK",
    "EXIT_USAGE_ERROR",
    "EXIT_STALE_CONTEXT",
    "EXIT_PATCH_FAILS",
    "EXIT_SCOPE_VIOLATION",
]
