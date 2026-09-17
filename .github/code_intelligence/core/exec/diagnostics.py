"""Turn a build/test/lint tool's raw stdout+stderr into structured, bounded diagnostics.

A failing build or test run is exactly the situation where dumping the raw
log is tempting and wrong: a real CMake+Ninja rebuild can produce tens of
kilobytes of output for two real errors, and a pytest run's full output
repeats every failure's traceback twice (once inline, once in the summary).
Each parser here extracts the handful of facts an agent actually needs --
what failed, and where -- and keeps the raw text only as a capped tail
(`log_tail`) for when even the structured answer is not enough.

These are text-format parsers, not AST/LSP tools -- deliberately: GCC,
Clang, ctest and pytest each have one well-known, stable output grammar
that every version of that tool already produces, so parsing it costs
nothing extra to run and needs no per-tool integration. This is the
opposite case from finding references or renaming a symbol, where the
textual heuristic is the wrong engine; here the tool's own text output
*is* the authoritative answer, just formatted for a terminal instead of a
program.
"""

import re
from typing import Any

#: Caps applied to every `log_tail`: enough to see the failure in context,
#: never enough to be "the log" again.
_MAX_LOG_TAIL_LINES = 60
_MAX_LOG_TAIL_BYTES = 4000

#: GCC/Clang: "path/to/file.cpp:137:18: error: message" (column optional;
#: MSVC-style "note:" lines are recognized but dropped -- supplementary
#: detail, not worth a slot in a capped summary).
_COMPILER_DIAG = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?:(?P<column>\d+):)?\s*"
    r"(?P<level>error|warning|note):\s*(?P<message>.+)$",
    re.MULTILINE,
)

#: pytest: "FAILED tests/test_x.py::test_name - AssertionError: ..."
_PYTEST_FAILED = re.compile(r"^FAILED (?P<nodeid>\S+)(?: - (?P<reason>.+))?$", re.MULTILINE)

#: pytest's final summary line, e.g. "2 failed, 40 passed in 1.23s".
_PYTEST_SUMMARY = re.compile(
    r"(?P<summary>\d+ (?:failed|passed|error|skipped|deselected|xfailed|xpassed)"
    r"(?:, \d+ (?:failed|passed|error|skipped|deselected|xfailed|xpassed))*)"
    r"\s+in\s+[\d.]+s"
)
_COUNT_TERM = re.compile(r"(\d+) (\w+)")

#: ctest: "  50% tests passed, 3 tests failed out of 6" when at least one
#: test fails, but "100% tests passed out of 1" (no ", N tests failed"
#: clause at all) when every test passes -- the clause is optional here to
#: cover both, with `failed` defaulting to 0 when it is absent.
#: Failed-test lines look like "  3 - SomeTest.Case (Failed)".
_CTEST_SUMMARY = re.compile(
    r"(?P<passed_pct>[\d.]+)% tests passed(?:, (?P<failed>\d+) tests failed)? out of (?P<total>\d+)"
)
_CTEST_FAILED_LINE = re.compile(r"^\s*\d+\s+-\s+(?P<name>\S+)\s+\(Failed\)", re.MULTILINE)

#: ruff's `--output-format=concise`: "path:line:col: CODE message".
_RUFF_ISSUE = re.compile(
    r"^(?P<file>[^:\n]+):(?P<line>\d+):(?P<column>\d+): (?P<code>\S+) (?P<message>.+)$",
    re.MULTILINE,
)

#: black's `--check --diff` prints "would reformat <path>" per file, plus a
#: final "N file(s) would be reformatted" line.
_BLACK_WOULD_REFORMAT = re.compile(r"^would reformat (?P<file>.+)$", re.MULTILINE)


def log_tail(text: str) -> str:
    """The last `_MAX_LOG_TAIL_LINES` lines of `text`, further capped by bytes."""
    lines = text.splitlines()[-_MAX_LOG_TAIL_LINES:]
    tail = "\n".join(lines)
    return tail[-_MAX_LOG_TAIL_BYTES:]


def _counts_from_summary(summary: str) -> dict[str, int]:
    return {word: int(count) for count, word in _COUNT_TERM.findall(summary)}


def parse_compiler_output(stdout: str, stderr: str) -> dict[str, Any]:
    """Extract `{file, line, column, message}` diagnostics from GCC/Clang-style output."""
    combined = f"{stdout}\n{stderr}"
    errors: list[dict[str, Any]] = []
    warnings: list[dict[str, Any]] = []
    for match in _COMPILER_DIAG.finditer(combined):
        level = match.group("level")
        if level == "note":
            continue
        entry = {
            "file": match.group("file"),
            "line": int(match.group("line")),
            "column": int(match.group("column")) if match.group("column") else None,
            "message": match.group("message").strip(),
        }
        (errors if level == "error" else warnings).append(entry)
    return {"errors": errors, "warnings": warnings, "log_tail": log_tail(combined)}


def parse_pytest_output(stdout: str, stderr: str) -> dict[str, Any]:
    """Extract failed test node IDs + reasons and the summary counts from pytest output."""
    combined = f"{stdout}\n{stderr}"
    failures = [
        {"test": match.group("nodeid"), "reason": (match.group("reason") or "").strip() or None}
        for match in _PYTEST_FAILED.finditer(combined)
    ]
    summary_match = _PYTEST_SUMMARY.search(combined)
    counts = _counts_from_summary(summary_match.group("summary")) if summary_match else {}
    return {"failures": failures, "counts": counts, "log_tail": log_tail(combined)}


def parse_ctest_output(stdout: str, stderr: str) -> dict[str, Any]:
    """Extract failed test names and the pass/fail/total counts from ctest output."""
    combined = f"{stdout}\n{stderr}"
    failed_tests = [match.group("name") for match in _CTEST_FAILED_LINE.finditer(combined)]
    summary_match = _CTEST_SUMMARY.search(combined)
    counts = None
    if summary_match:
        total = int(summary_match.group("total"))
        failed = int(summary_match.group("failed") or 0)
        counts = {"total": total, "failed": failed, "passed": total - failed}
    return {"failed_tests": failed_tests, "counts": counts, "log_tail": log_tail(combined)}


def parse_ruff_check_output(stdout: str) -> list[dict[str, Any]]:
    """Extract `{file, line, column, code, message}` issues from `ruff check --output-format=concise`."""
    return [
        {
            "file": match.group("file"),
            "line": int(match.group("line")),
            "column": int(match.group("column")),
            "code": match.group("code"),
            "message": match.group("message").strip(),
        }
        for match in _RUFF_ISSUE.finditer(stdout)
    ]


def parse_black_check_output(stderr: str) -> list[str]:
    """Extract the list of files black's `--check` reports as needing reformatting."""
    return [match.group("file").strip() for match in _BLACK_WOULD_REFORMAT.finditer(stderr)]


__all__ = [
    "log_tail",
    "parse_compiler_output",
    "parse_pytest_output",
    "parse_ctest_output",
    "parse_ruff_check_output",
    "parse_black_check_output",
]
