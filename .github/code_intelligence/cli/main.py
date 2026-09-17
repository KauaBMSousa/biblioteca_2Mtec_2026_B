"""`code-intelligence` CLI: index|status|query|symbol|context|violations|range|diff.

Every subcommand does a one-shot, in-process `Workspace.open()` call — no
daemon dependency (that's Phase B). Output is JSON on stdout, matching the
agent-consumption convention the underlying `Workspace` query methods
already return.

Exit codes for `index`/`violations`/`query` (severity-based, mirroring the
old tool): 0 OK, 1 WARNING-only, 2 HIGH/CRITICAL/VERY_CRITICAL present.
Every other subcommand: 0 success, 1 not-found/usage error.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from code_intelligence.core.diagnostics.violation import Severity
from code_intelligence.core.git.diff_report import STAGED, WORKING_TREE
from code_intelligence.core.workspace import Workspace
from code_intelligence.core.workspace.security import PathTraversalError
from code_intelligence.daemon import lifecycle

_EXIT_OK = 0
_EXIT_WARNING = 1
_EXIT_BLOCKING = 2
_EXIT_ERROR = 3


def _print(payload: Any) -> None:
    """Print one JSON payload, pretty and stable-keyed."""
    print(json.dumps(payload, indent=2, sort_keys=False, default=str))


def _severity_exit_code(violations: list[dict[str, Any]]) -> int:
    """Map the most serious violation severity present to an exit code."""
    if not violations:
        return _EXIT_OK
    max_severity = max(Severity[v["severity"]] for v in violations)
    if max_severity == Severity.WARNING:
        return _EXIT_WARNING
    if max_severity >= Severity.HIGH:
        return _EXIT_BLOCKING
    return _EXIT_OK


def _cmd_index(args: argparse.Namespace) -> int:
    with Workspace.open(args.root) as workspace:
        stats = workspace.index(semantic_refresh=args.semantic)
        payload = {
            "files_total": stats.files_total,
            "files_reused": stats.files_reused,
            "files_reparsed": stats.files_reparsed,
            "files_removed": stats.files_removed,
            "symbols_total": stats.symbols_total,
            "diagnostics_total": stats.diagnostics_total,
            "duration_seconds": round(stats.duration_seconds, 3),
        }
        if stats.semantic is not None:
            payload["semantic"] = stats.semantic
        _print(payload)
        violations = workspace.get_violations()
        return _severity_exit_code(violations)


def _cmd_status(args: argparse.Namespace) -> int:
    with Workspace.open(args.root) as workspace:
        _print(workspace.status())
        return _EXIT_OK


def _cmd_query(args: argparse.Namespace) -> int:
    with Workspace.open(args.root) as workspace:
        if args.file:
            payload = workspace.get_file_structure(args.file)
            if payload is None:
                print(f"error: no indexed file matches {args.file!r} (run `index` first?)", file=sys.stderr)
                return _EXIT_ERROR
        else:
            payload = workspace.get_workspace_structure()
        _print(payload)
        return _EXIT_OK


def _cmd_symbol(args: argparse.Namespace) -> int:
    with Workspace.open(args.root) as workspace:
        payload = workspace.find_symbol(args.value, args.file)
        if payload is None:
            print(f"error: no symbol matched {args.value!r}" + (f" in {args.file}" if args.file else ""), file=sys.stderr)
            return _EXIT_ERROR
        _print(payload)
        return _EXIT_OK


def _cmd_context(args: argparse.Namespace) -> int:
    with Workspace.open(args.root) as workspace:
        payload = workspace.get_context(args.symbol_id, max_lines=args.max_lines, max_bytes=args.max_bytes)
        if payload is None:
            print(f"error: no symbol matched {args.symbol_id!r}", file=sys.stderr)
            return _EXIT_ERROR
        _print(payload)
        return _EXIT_OK


def _cmd_violations(args: argparse.Namespace) -> int:
    with Workspace.open(args.root) as workspace:
        violations = workspace.get_violations(severity=args.severity)
        _print(violations)
        return _severity_exit_code(violations)


def _cmd_range(args: argparse.Namespace) -> int:
    match = re.match(r"^(?P<file>.+):(?P<start>\d+)-(?P<end>\d+)$", args.spec)
    if not match:
        print(f"error: --range must look like FILE:START-END, got: {args.spec!r}", file=sys.stderr)
        return _EXIT_ERROR
    start, end = int(match.group("start")), int(match.group("end"))
    if start < 1 or end < start:
        print(f"error: invalid line span: {args.spec!r}", file=sys.stderr)
        return _EXIT_ERROR
    with Workspace.open(args.root) as workspace:
        try:
            payload = workspace.read_range(match.group("file"), start, end)
        except PathTraversalError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return _EXIT_ERROR
        except OSError as exc:
            print(f"error: could not read {match.group('file')}: {exc}", file=sys.stderr)
            return _EXIT_ERROR
        _print(payload)
        return _EXIT_OK


def _cmd_diff(args: argparse.Namespace) -> int:
    with Workspace.open(args.root) as workspace:
        payload = workspace.diff(args.ref)
        _print(payload)
        return _EXIT_BLOCKING if payload["regressions"] else _EXIT_OK


def _cmd_daemon(args: argparse.Namespace) -> int:
    if args.daemon_command == "start":
        result = lifecycle.start(args.root)
    elif args.daemon_command == "stop":
        result = lifecycle.stop(args.root)
    elif args.daemon_command == "status":
        result = lifecycle.status(args.root)
    else:  # pragma: no cover - argparse enforces `choices`
        raise AssertionError(args.daemon_command)
    _print(result)
    return _EXIT_OK if result.get("status") not in ("failed",) else _EXIT_ERROR


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="code-intelligence",
        description="Persistent, incremental, symbol-oriented code intelligence platform.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    index_parser = subparsers.add_parser("index", help="Run an incremental index pass over a workspace.")
    index_parser.add_argument("root", nargs="?", default=".", help="Workspace root (default: cwd).")
    index_parser.add_argument(
        "--semantic",
        action="store_true",
        help=(
            "Also re-extract exact references for whatever this pass made stale. "
            "Off by default because it is expensive (~13s per C++ translation unit), "
            "but skipping it leaves the exact edges pointing at line numbers that moved."
        ),
    )
    index_parser.set_defaults(func=_cmd_index)

    status_parser = subparsers.add_parser("status", help="Print workspace identity + index metadata.")
    status_parser.add_argument("root", nargs="?", default=".", help="Workspace root (default: cwd).")
    status_parser.set_defaults(func=_cmd_status)

    query_parser = subparsers.add_parser("query", help="L1/L2: workspace structure, or one file's structure.")
    query_parser.add_argument("root", nargs="?", default=".", help="Workspace root (default: cwd).")
    query_parser.add_argument("--file", metavar="RELPATH", help="Narrow to one file's symbol structure (L2).")
    query_parser.set_defaults(func=_cmd_query)

    symbol_parser = subparsers.add_parser("symbol", help="L3: one symbol's full metadata (no source content).")
    symbol_parser.add_argument("value", metavar="VALUE", help="symbol_id, or a bare/qualified name with --file.")
    symbol_parser.add_argument("--root", default=".", help="Workspace root (default: cwd).")
    symbol_parser.add_argument("--file", metavar="RELPATH", help="Scope a bare-name lookup to this file.")
    symbol_parser.set_defaults(func=_cmd_symbol)

    context_parser = subparsers.add_parser("context", help="L4/5: one symbol's source content, budgeted.")
    context_parser.add_argument("symbol_id", metavar="SYMBOL_ID")
    context_parser.add_argument("--root", default=".", help="Workspace root (default: cwd).")
    context_parser.add_argument("--max-lines", type=int, default=None, dest="max_lines")
    context_parser.add_argument("--max-bytes", type=int, default=None, dest="max_bytes")
    context_parser.set_defaults(func=_cmd_context)

    violations_parser = subparsers.add_parser("violations", help="Every diagnostic at/above --severity.")
    violations_parser.add_argument("root", nargs="?", default=".", help="Workspace root (default: cwd).")
    violations_parser.add_argument(
        "--severity", choices=[s.name for s in Severity if s != Severity.OK], default=None
    )
    violations_parser.set_defaults(func=_cmd_violations)

    range_parser = subparsers.add_parser("range", help="Read an exact line range: FILE:START-END.")
    range_parser.add_argument("spec", metavar="FILE:START-END")
    range_parser.add_argument("--root", default=".", help="Workspace root (default: cwd).")
    range_parser.set_defaults(func=_cmd_range)

    diff_parser = subparsers.add_parser(
        "diff", help=f"Compare against a ref, '{WORKING_TREE}', or '{STAGED}' (fixme §35)."
    )
    diff_parser.add_argument("ref", metavar="REF")
    diff_parser.add_argument("--root", default=".", help="Workspace root (default: cwd).")
    diff_parser.set_defaults(func=_cmd_diff)

    daemon_parser = subparsers.add_parser("daemon", help="start|status|stop the per-workspace daemon.")
    daemon_parser.add_argument("daemon_command", choices=["start", "status", "stop"])
    daemon_parser.add_argument("root", nargs="?", default=".", help="Workspace root (default: cwd).")
    daemon_parser.set_defaults(func=_cmd_daemon)

    return parser


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint. Returns the process exit code; never raises for user errors."""
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(getattr(args, "root", ".")).resolve()
    if not root.is_dir():
        print(f"error: workspace root does not exist or is not a directory: {root}", file=sys.stderr)
        return _EXIT_ERROR
    args.root = root
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
