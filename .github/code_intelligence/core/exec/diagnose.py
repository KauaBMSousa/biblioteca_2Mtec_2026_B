"""Run the build/tests and return only the localized failure facts (ENHANCEME §I, §J).

`diagnose()` is the compression layer the raw runners deliberately leave to
a caller: it runs `run_build` / `run_tests`, keeps their already-structured
`errors` / `failures`, and does the one enrichment an index makes cheap —
mapping each `file:line` to the symbol whose span contains it, so the agent
learns "the type error is in `Tensor::backward`" without a `get_symbol`
round trip per error. The capped `log_tail` is carried only when nothing
structured could be extracted from a failing run.

It raises nothing for a failing build or failing tests — those are the
normal answer — only for the infrastructure problems the runners raise
(`UnsupportedLanguageError`, `ToolNotInstalledError`, `TimeoutError`).
"""

from pathlib import Path
from typing import Any

from code_intelligence.core import exec as exec_module
from code_intelligence.core.exec.errors import ToolNotInstalledError, UnsupportedLanguageError
from code_intelligence.core.index.store import IndexStore


def _enclosing_symbol(store: IndexStore, file: str, line: int) -> str | None:
    """Qualified name of the innermost indexed symbol whose span contains `file:line`."""
    best: dict[str, Any] | None = None
    for row in store.list_symbols_for_file(file):
        start, end = row["start_line"], row["end_line"]
        if start is None or end is None or not (start <= line <= end):
            continue
        if best is None or row["start_line"] >= best["start_line"]:
            best = row
    if best is None:
        return None
    return best["qualified_name"] or best["name"]


def _localize(store: IndexStore, entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add a `symbol` key to every `{file, line, message}` diagnostic entry."""
    out: list[dict[str, Any]] = []
    for entry in entries:
        localized = dict(entry)
        if entry.get("file") and entry.get("line"):
            localized["symbol"] = _enclosing_symbol(store, entry["file"], entry["line"])
        out.append(localized)
    return out


def _diagnose_build(root: Path, store: IndexStore, timeout: int | None) -> dict[str, Any] | None:
    try:
        result = exec_module.run_build(root, None, **({} if timeout is None else {"timeout": timeout}))
    except (UnsupportedLanguageError, ToolNotInstalledError) as exc:
        return {"status": "SKIPPED", "reason": str(exc)}
    passed = result["status"] == "PASSED"
    return {
        "status": result["status"],
        "error_count": result["error_count"],
        "warning_count": result["warning_count"],
        "errors": _localize(store, result["errors"]),
        "log_tail": None if (passed or result["errors"]) else result.get("log_tail"),
    }


def _diagnose_tests(
    root: Path, store: IndexStore, toolchain: str | None, filter: str | None, timeout: int | None
) -> dict[str, Any] | None:
    try:
        result = exec_module.run_tests(
            root, toolchain, filter, **({} if timeout is None else {"timeout": timeout})
        )
    except (UnsupportedLanguageError, ToolNotInstalledError, ValueError) as exc:
        return {"status": "SKIPPED", "reason": str(exc)}
    passed = result["status"] == "PASSED"
    # pytest -> `failures` [{test, reason}]; ctest -> `failed_tests` [name].
    failures = result.get("failures")
    if failures is None:
        failures = [{"test": name} for name in result.get("failed_tests", [])]
    section = {
        "status": result["status"],
        "tool": result["tool"],
        "counts": result.get("counts"),
        "failures": failures,
        "log_tail": None if (passed or failures) else result.get("log_tail"),
    }
    if len(failures) >= 3:
        from code_intelligence.core.exec.cluster import cluster_failures

        section["clusters"] = cluster_failures(failures)["clusters"]
    return section


def _diagnose_static_analysis(store: IndexStore) -> dict[str, Any] | None:
    """The C++ static-analysis findings the index already holds (cppcheck). No re-run."""
    rows = [
        row for row in store.list_diagnostics()
        if row["rule"].startswith("CPPCHECK_")
    ]
    if not rows:
        return None
    by_severity: dict[str, int] = {}
    findings: list[dict[str, Any]] = []
    for row in rows:
        name = _severity_name(row["severity"])
        by_severity[name] = by_severity.get(name, 0) + 1
        findings.append({
            "code": row["rule"],
            "severity": name,
            "file": row["file_path"],
            "line": row["start_line"],
            "symbol": _enclosing_symbol(store, row["file_path"], row["start_line"]),
            "message": row["message"],
            "confidence": row["confidence"],
        })
    run_failed = any(r["rule"] == "CPPCHECK_RUN_FAILED" for r in rows)
    return {
        "tool": "cppcheck",
        "status": "FAILED" if run_failed else ("ISSUES_FOUND" if findings else "PASSED"),
        "finding_count": len(findings),
        "by_severity": by_severity,
        "findings": findings[:200],
        "note": "from the persistent index — run `cppcheck` for a fresh targeted check",
    }


def _severity_name(value: int) -> str:
    from code_intelligence.core.diagnostics.violation import Severity

    return Severity(value).name


def diagnose(
    root: Path,
    store: IndexStore,
    scope: str = "all",
    *,
    toolchain: str | None = None,
    filter: str | None = None,
    timeout: int | None = None,
) -> dict[str, Any]:
    """Run `scope` and return localized failures only.

    `scope` ∈ `"build"`, `"tests"`, `"analysis"` (the index's C++
    static-analysis findings), or `"all"`.
    """
    if scope not in ("all", "build", "tests", "analysis"):
        raise ValueError(f"scope must be 'all', 'build', 'tests' or 'analysis', not {scope!r}")

    build = _diagnose_build(root, store, timeout) if scope in ("all", "build") else None
    tests = (
        _diagnose_tests(root, store, toolchain, filter, timeout)
        if scope in ("all", "tests")
        else None
    )
    analysis = _diagnose_static_analysis(store) if scope in ("all", "analysis") else None

    def _failed(section: dict[str, Any] | None) -> bool:
        return bool(section) and section["status"] == "FAILED"

    entries = _index_entries(build, tests)
    _tag_ids(build, tests, entries)
    try:
        store.diagnostic_context_replace(entries)
    except Exception:  # noqa: BLE001 - the id table is a convenience, not correctness
        pass

    result = {
        "scope": scope,
        "ok": not (_failed(build) or _failed(tests) or _failed(analysis)),
        "build": build,
        "tests": tests,
    }
    if analysis is not None:
        result["static_analysis"] = analysis
    return result


def _diag_id(kind: str, file: Any, line: Any, message: Any) -> str:
    import hashlib

    payload = f"{kind}|{file}|{line}|{message}"
    return "d" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:8]


def _index_entries(build: dict | None, tests: dict | None) -> list[dict[str, Any]]:
    """Flatten build errors + test failures into id-tagged diagnostic-context rows."""
    entries: list[dict[str, Any]] = []
    for err in (build or {}).get("errors", []) if build else []:
        entries.append(
            {
                "diagnostic_id": _diag_id("build_error", err.get("file"), err.get("line"), err.get("message")),
                "kind": "build_error",
                "file": err.get("file"),
                "line": err.get("line"),
                "symbol": err.get("symbol"),
                "message": err.get("message"),
            }
        )
    for fail in (tests or {}).get("failures", []) if tests else []:
        entries.append(
            {
                "diagnostic_id": _diag_id("test_failure", None, None, fail.get("test")),
                "kind": "test_failure",
                "file": None,
                "line": None,
                "symbol": None,
                "message": f"{fail.get('test')}: {fail.get('reason') or ''}".strip(),
            }
        )
    return entries


def _tag_ids(build: dict | None, tests: dict | None, entries: list[dict[str, Any]]) -> None:
    """Write the computed `diagnostic_id` back onto the returned build/test entries."""
    for err in (build or {}).get("errors", []) if build else []:
        err["diagnostic_id"] = _diag_id("build_error", err.get("file"), err.get("line"), err.get("message"))
    for fail in (tests or {}).get("failures", []) if tests else []:
        fail["diagnostic_id"] = _diag_id("test_failure", None, None, fail.get("test"))


__all__ = ["diagnose"]
