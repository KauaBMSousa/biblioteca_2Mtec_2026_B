"""C++ static analysis via Cppcheck, folded into the persistent index.

Cppcheck answers a different question from the structural rules: not "is
this file too long / undocumented" but "does this code have a bug" — a
null dereference, a use of an uninitialised value, an out-of-bounds index,
a leak, a dangerous cast. It runs as a project-wide index pass (next to
duplication and test coverage): every finding becomes a `Violation` row
keyed `CPPCHECK_<id>`, attributed to the enclosing symbol, and from there
flows through `get_violations`, `workspace_snapshot`, `diff` regressions
and `context_for_task` like any other diagnostic.

Availability is reported, never guessed: if `cppcheck` is not on PATH the
pass is skipped and `check()` returns `None`. Cppcheck without a full
build cannot be certain, so its findings are `medium` confidence (`low`
when cppcheck itself marks them inconclusive) — never `high`.

Incremental: a `--cppcheck-build-dir` cache under
`.code-intelligence/cppcheck/` means re-analysing the whole C++ file set
only re-checks the files that actually changed, so the indexer can run
this pass on every reindex where any C++ file moved without paying to
re-analyse the rest.
"""

import shutil
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.index.store import IndexStore

#: Every diagnostic this pass emits starts with this — one prefix to delete
#: on a re-run, one prefix to filter on in `get_violations`.
RULE_PREFIX = "CPPCHECK_"

#: cppcheck severity -> our Severity. `information`/`debug` are not findings
#: (missing-include chatter, internal notes) and map to None = dropped.
_SEVERITY_MAP: dict[str, Severity | None] = {
    "error": Severity.HIGH,
    "warning": Severity.HIGH,
    "performance": Severity.WARNING,
    "portability": Severity.WARNING,
    "style": Severity.WARNING,
    "information": None,
    "debug": None,
}

#: Noise suppressed unconditionally: we give cppcheck an explicit file list
#: and best-effort `-I` dirs, not a build, so "can't find <vector>" is
#: expected and not a code defect. `unmatchedSuppression` would then fire
#: about these suppressions on files that had no such problem.
_BASE_SUPPRESSIONS = ("missingInclude", "missingIncludeSystem", "unmatchedSuppression")

_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "checks": ["warning", "style", "performance", "portability"],
    "inconclusive": False,
    "unused_functions": False,
    "severity": "WARNING",  # floor: nothing from cppcheck is reported below this
    "suppress": [],
    "extra_args": [],
    "max_files": 2000,
    "timeout_seconds": 300,
}


class CppcheckUnavailable(RuntimeError):
    """cppcheck is not installed / not runnable here."""


@dataclass(slots=True)
class AnalyzeResult:
    """One cppcheck run over a file set."""

    findings: list[dict[str, Any]] = field(default_factory=list)
    files_analyzed: int = 0
    tool_version: str | None = None
    #: Set only for an infrastructure failure (crash, timeout, bad XML) —
    #: an ordinary run that found nothing has `error=None` and `findings=[]`.
    error: str | None = None


def config_for(config: dict) -> dict[str, Any]:
    """The effective `cppcheck` config block (defaults merged under the override)."""
    merged = dict(_DEFAULTS)
    merged.update(config.get("cppcheck") or {})
    return merged


def available() -> tuple[bool, str | None]:
    """`(is_runnable, version_string)` — never raises."""
    path = shutil.which("cppcheck")
    if path is None:
        return False, None
    try:
        out = subprocess.run(
            [path, "--version"], capture_output=True, text=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False, None
    return True, out.stdout.strip() or None


def _severity_floor(name: str) -> Severity:
    try:
        return Severity[name.upper()]
    except KeyError as exc:
        valid = ", ".join(level.name for level in Severity)
        raise ValueError(
            f"cppcheck.severity: unknown severity {name!r}; expected one of {valid}"
        ) from exc


_HEADER_SUFFIXES = ("h", "hpp", "hh", "hxx")


def _include_dirs(root: Path, files: list[str]) -> list[str]:
    """Best-effort `-I` set: every directory that holds an indexed header, deduped."""
    dirs: set[Path] = set()
    for rel in files:
        if rel.rsplit(".", 1)[-1].lower() in _HEADER_SUFFIXES:
            parent = (root / rel).parent
            dirs.add(parent)
            dirs.add(parent.parent)
    return sorted(str(d) for d in dirs if d.is_dir())[:64]


def _build_command(
    root: Path, files: list[str], settings: dict[str, Any], build_dir: Path | None
) -> list[str]:
    import os

    checks = list(settings.get("checks") or ["warning"])
    if settings.get("unused_functions") and "unusedFunction" not in checks:
        checks.append("unusedFunction")

    cmd = [
        "cppcheck",
        "--xml",
        "--xml-version=2",
        "--quiet",
        "--language=c++",
        f"--enable={','.join(checks)}",
        "--relative-paths",
    ]
    if settings.get("inconclusive"):
        cmd.append("--inconclusive")
    # `unusedFunction` is a whole-program check and is silently dropped under
    # `-j`; only parallelise when it is off.
    if "unusedFunction" not in checks:
        cmd.append(f"-j{max(1, os.cpu_count() or 2)}")
    if build_dir is not None:
        build_dir.mkdir(parents=True, exist_ok=True)
        cmd.append(f"--cppcheck-build-dir={build_dir}")
    for supp in (*_BASE_SUPPRESSIONS, *settings.get("suppress", [])):
        cmd.append(f"--suppress={supp}")
    for inc in _include_dirs(root, files):
        cmd.append(f"-I{inc}")
    cmd.extend(settings.get("extra_args", []))
    cmd.extend(files)
    return cmd


def analyze(
    root: Path,
    files: list[str],
    config: dict,
    *,
    build_dir: Path | None = None,
) -> AnalyzeResult:
    """Run cppcheck over `files` (workspace-relative) and return the parsed findings.

    Raises `CppcheckUnavailable` if cppcheck is not installed. A failing
    cppcheck run (crash, timeout) is reported in `AnalyzeResult.error`, not
    raised — a project-wide index pass must never turn a flaky analyzer
    into a failed index.
    """
    ok, version = available()
    if not ok:
        raise CppcheckUnavailable("cppcheck is not on PATH")

    settings = config_for(config)
    files = [f for f in files if (root / f).is_file()][: settings["max_files"]]
    if not files:
        return AnalyzeResult(tool_version=version)

    cmd = _build_command(root, files, settings, build_dir)
    try:
        proc = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True,
            timeout=settings["timeout_seconds"], check=False,
        )
    except subprocess.TimeoutExpired:
        return AnalyzeResult(
            files_analyzed=len(files), tool_version=version,
            error=f"cppcheck exceeded its {settings['timeout_seconds']}s timeout",
        )
    except OSError as exc:
        return AnalyzeResult(tool_version=version, error=f"cppcheck could not run: {exc}")

    # --xml writes results to stderr; stdout carries only progress (silenced
    # by --quiet). A non-empty stdout with an empty stderr and a non-zero
    # exit is cppcheck itself failing.
    xml_text = proc.stderr.strip()
    if not xml_text:
        if proc.returncode != 0:
            return AnalyzeResult(
                files_analyzed=len(files), tool_version=version,
                error=f"cppcheck exited {proc.returncode}: {proc.stdout.strip()[:400]}",
            )
        return AnalyzeResult(files_analyzed=len(files), tool_version=version)

    try:
        findings = _parse_xml(xml_text)
    except ET.ParseError as exc:
        return AnalyzeResult(
            files_analyzed=len(files), tool_version=version,
            error=f"cppcheck XML was not parseable: {exc}",
        )
    return AnalyzeResult(
        findings=findings, files_analyzed=len(files), tool_version=version
    )


def _parse_xml(xml_text: str) -> list[dict[str, Any]]:
    """Flatten cppcheck's `<results><errors><error>` into finding dicts."""
    # cppcheck can print a non-XML preprocessor note before the document;
    # slice from the real root so it still parses.
    start = xml_text.find("<results")
    if start > 0:
        xml_text = xml_text[start:]
    root_el = ET.fromstring(xml_text)
    out: list[dict[str, Any]] = []
    for error in root_el.iter("error"):
        locations = error.findall("location")
        if not locations:
            continue  # a project-scope note with no site — not actionable here
        primary = locations[0]
        file_attr = primary.get("file")
        line_attr = primary.get("line")
        if not file_attr or not line_attr:
            continue
        try:
            line = int(line_attr)
        except ValueError:
            continue
        out.append({
            "id": error.get("id", "unknown"),
            "cppcheck_severity": error.get("severity", "style"),
            "message": (error.get("msg") or "").strip(),
            "verbose": (error.get("verbose") or "").strip(),
            "cwe": error.get("cwe"),
            "inconclusive": error.get("inconclusive") == "true",
            "file": file_attr,
            "line": max(1, line),
            "column": _int_or_none(primary.get("column")),
            "symbol": (error.findtext("symbol") or None),
            "trace": [
                {"file": loc.get("file"), "line": _int_or_none(loc.get("line")),
                 "info": loc.get("info")}
                for loc in locations[1:]
            ],
        })
    return out


def _int_or_none(value: str | None) -> int | None:
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


def _enclosing_symbol_id(store: IndexStore, file: str, line: int) -> str | None:
    """The innermost indexed symbol whose span contains `file:line`."""
    best: dict[str, Any] | None = None
    for row in store.list_symbols_for_file(file):
        start, end = row["start_line"], row["end_line"]
        if start is None or end is None or not (start <= line <= end):
            continue
        if best is None or row["start_line"] >= best["start_line"]:
            best = row
    return best["symbol_id"] if best else None


def to_violations(
    store: IndexStore, findings: list[dict[str, Any]], config: dict
) -> list[Violation]:
    """Map cppcheck findings to `Violation` rows, keeping only indexed C++ files.

    `diagnostics.file_path` is a foreign key into `files`; a finding in a
    system header or an excluded file cannot be stored and is dropped.
    """
    floor = _severity_floor(config_for(config).get("severity") or "WARNING")
    indexed_cpp = {
        row["path"] for row in store.list_files() if row["language"] == "cpp"
    }
    violations: list[Violation] = []
    for f in findings:
        if f["file"] not in indexed_cpp:
            continue
        severity = _SEVERITY_MAP.get(f["cppcheck_severity"])
        if severity is None or severity < floor:
            continue
        detail: dict[str, Any] = {
            "cppcheck_id": f["id"],
            "cppcheck_severity": f["cppcheck_severity"],
        }
        if f.get("cwe"):
            detail["cwe"] = f["cwe"]
        if f.get("verbose") and f["verbose"] != f["message"]:
            detail["explanation"] = f["verbose"]
        if f.get("trace"):
            detail["trace"] = f["trace"]
        if f.get("symbol"):
            detail["symbol"] = f["symbol"]
        violations.append(
            Violation(
                code=f"{RULE_PREFIX}{f['id']}",
                severity=severity,
                file=f["file"],
                line=f["line"],
                end_line=f["line"],
                message=f"cppcheck ({f['cppcheck_severity']}): {f['message']}",
                detail=detail,
                symbol_id=_enclosing_symbol_id(store, f["file"], f["line"]),
                column=f.get("column"),
                # cppcheck reasons without the real build's includes/defines,
                # so even its `error`-severity findings are heuristics here.
                confidence="low" if f["inconclusive"] else "medium",
            )
        )
    return violations


def check(
    store: IndexStore,
    root: Path,
    config: dict,
    *,
    build_dir: Path | None = None,
) -> list[Violation] | None:
    """The indexer entry point: analyse every indexed C++ file, or `None` if skipped.

    Returns `None` when the pass did not run (disabled, no C++, or cppcheck
    not installed) — the caller must then leave any existing `CPPCHECK_*`
    rows untouched. An empty list means it ran and found nothing.
    """
    settings = config_for(config)
    if not settings.get("enabled", True):
        return None

    cpp_files = sorted(
        row["path"] for row in store.list_files() if row["language"] == "cpp"
    )
    if not cpp_files:
        return None

    ok, _ = available()
    if not ok:
        return None

    result = analyze(root, cpp_files, config, build_dir=build_dir)
    if result.error:
        # Surface the infra failure as one workspace-scoped WARNING rather
        # than silently producing zero findings (which reads as "clean").
        return [
            Violation(
                code=f"{RULE_PREFIX}RUN_FAILED",
                severity=Severity.WARNING,
                file=cpp_files[0],
                line=1,
                end_line=1,
                message=f"cppcheck could not complete: {result.error}",
                detail={"scope": "workspace"},
                confidence="high",
            )
        ]
    return to_violations(store, result.findings, config)


__all__ = [
    "RULE_PREFIX",
    "AnalyzeResult",
    "CppcheckUnavailable",
    "analyze",
    "available",
    "check",
    "config_for",
    "to_violations",
]
