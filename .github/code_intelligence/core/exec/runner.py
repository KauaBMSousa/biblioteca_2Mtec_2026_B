"""Run a project's build/test/lint/format tool and return a structured, bounded result.

Each function auto-detects the toolchain via `toolchain.detect_toolchain`,
picks the one tool that answers the question -- raising `ToolNotInstalledError`
or `UnsupportedLanguageError` if none is usable, rather than silently doing
nothing or guessing -- runs it under a timeout, and classifies the outcome.
A failing build or test run is a normal, expected result (`status:
"FAILED"`, with structured `errors`/`failed_tests`); these functions raise
only for infrastructure problems: no usable tool, an unsupported project
layout, an ambiguous choice between multiple available toolchains, or a
timeout (`TimeoutError`, reused from the standard library rather than
adding a fourth exception name for the same fact).

Never returns the raw subprocess output in full: `log_tail` is always
capped (`diagnostics.log_tail`), and is only populated when the run did
not cleanly pass -- a passing run has nothing a caller needs from the log.
"""

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from code_intelligence.core.exec import diagnostics
from code_intelligence.core.exec.errors import ToolNotInstalledError, UnsupportedLanguageError
from code_intelligence.core.exec.toolchain import detect_toolchain

#: Generous default: a real CMake+Ninja rebuild or a full pytest run can
#: legitimately take minutes; the caller can always pass a tighter budget.
DEFAULT_TIMEOUT = 600


def _run(command: list[str], cwd: Path, timeout: int) -> tuple[int, str, str, float]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command, cwd=cwd, capture_output=True, text=True, timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(f"`{' '.join(command)}` exceeded its {timeout}s timeout") from exc
    except FileNotFoundError as exc:
        raise ToolNotInstalledError(f"`{command[0]}` could not be executed: {exc}") from exc
    return completed.returncode, completed.stdout, completed.stderr, time.monotonic() - started


def _find_cmake_build_dir(root: Path) -> Path | None:
    """The most recently configured CMake build directory under `root`.

    Checked under `out/build/*/` (this project's own preset layout) and
    plain `build/` (the common convention elsewhere). Multiple configured
    presets can coexist; the most recently modified `CMakeCache.txt` is
    almost always the one whose build the caller means -- typically the
    preset the caller (or the project's own CI/workflow) built most
    recently.
    """
    candidates = list(root.glob("out/build/*/CMakeCache.txt")) + [
        p for p in (root / "build" / "CMakeCache.txt",) if p.exists()
    ]
    if not candidates:
        return None
    candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0].parent


def run_build(root: Path, target: str | None = None, timeout: int = DEFAULT_TIMEOUT) -> dict[str, Any]:
    """Build the project with whatever build system it declares.

    Currently wired: CMake, via an already-configured build directory
    (`cmake --preset=...` must have run at least once). Raises
    `UnsupportedLanguageError` when no known build-system marker is
    present or no build directory has been configured yet, and
    `ToolNotInstalledError` when the marker is present but `cmake` itself
    is missing.
    """
    info = detect_toolchain(root)
    cmake = info["toolchains"].get("cmake")
    if cmake is None:
        raise UnsupportedLanguageError(f"no known build-system marker (CMakeLists.txt) under {root}")
    if not cmake["executables"].get("cmake"):
        raise ToolNotInstalledError("cmake is not on PATH")

    build_dir = _find_cmake_build_dir(root)
    if build_dir is None:
        raise UnsupportedLanguageError(
            f"CMakeLists.txt present under {root} but no configured build directory was found "
            "under out/build/*/ or build/ -- run `cmake --preset=<name>` first"
        )

    command = ["cmake", "--build", str(build_dir)]
    if target:
        command += ["--target", target]
    returncode, stdout, stderr, duration = _run(command, root, timeout)
    parsed = diagnostics.parse_compiler_output(stdout, stderr)
    passed = returncode == 0
    return {
        "tool": "cmake --build",
        "command": command,
        "build_dir": str(build_dir),
        "status": "PASSED" if passed else "FAILED",
        "exit_code": returncode,
        "duration_seconds": round(duration, 3),
        "error_count": len(parsed["errors"]),
        "warning_count": len(parsed["warnings"]),
        "errors": parsed["errors"],
        "warnings": parsed["warnings"],
        "log_tail": None if passed else parsed["log_tail"],
    }


def _test_capable_toolchains(info: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: toolchain
        for name, toolchain in info["toolchains"].items()
        if name in ("cmake", "python") and toolchain["available"]
    }


def run_tests(
    root: Path, toolchain: str | None = None, filter: str | None = None, timeout: int = DEFAULT_TIMEOUT
) -> dict[str, Any]:
    """Run the project's test suite.

    Auto-picks the toolchain when exactly one is available and test-capable
    (`cmake` -> ctest, `python` -> pytest). With more than one, `toolchain`
    must name which to run -- guessing which suite the caller means would
    silently skip the other, exactly the "0 callers" misreading
    `core.semantic` refuses to produce for references.

    `filter` narrows the run: a ctest `-R` regex, or a pytest `-k`
    expression, depending on which toolchain runs.
    """
    info = detect_toolchain(root)
    candidates = _test_capable_toolchains(info)

    if toolchain:
        if toolchain not in info["toolchains"]:
            raise UnsupportedLanguageError(f"no {toolchain!r} toolchain detected under {root}")
        chosen = toolchain
    elif len(candidates) == 1:
        chosen = next(iter(candidates))
    elif not candidates:
        raise UnsupportedLanguageError(
            f"no runnable test toolchain (cmake+ctest, or python+pytest) under {root}"
        )
    else:
        raise ValueError(
            f"multiple test toolchains available ({sorted(candidates)}); pass toolchain= to pick one"
        )

    if chosen == "cmake":
        return _run_ctest(root, info["toolchains"]["cmake"], filter, timeout)
    if chosen == "python":
        return _run_pytest(root, info["toolchains"]["python"], filter, timeout)
    raise UnsupportedLanguageError(f"run_tests does not know how to run the {chosen!r} toolchain yet")


def _run_ctest(
    root: Path, cmake_info: dict[str, Any], filter: str | None, timeout: int
) -> dict[str, Any]:
    if not cmake_info["executables"].get("ctest"):
        raise ToolNotInstalledError("ctest is not on PATH")
    build_dir = _find_cmake_build_dir(root)
    if build_dir is None:
        raise UnsupportedLanguageError(f"no configured CMake build directory under {root}")

    command = ["ctest", "--test-dir", str(build_dir), "--output-on-failure"]
    if filter:
        command += ["-R", filter]
    returncode, stdout, stderr, duration = _run(command, root, timeout)
    parsed = diagnostics.parse_ctest_output(stdout, stderr)
    passed = returncode == 0
    return {
        "tool": "ctest",
        "command": command,
        "status": "PASSED" if passed else "FAILED",
        "exit_code": returncode,
        "duration_seconds": round(duration, 3),
        "counts": parsed["counts"],
        "failed_tests": parsed["failed_tests"],
        "log_tail": None if passed else parsed["log_tail"],
    }


def _run_pytest(
    root: Path, python_info: dict[str, Any], filter: str | None, timeout: int
) -> dict[str, Any]:
    if not python_info["available"]:
        raise ToolNotInstalledError("pytest is not importable by this project's interpreter")

    interpreter = python_info["interpreter"]
    command = [interpreter, "-m", "pytest", "-q"]
    if filter:
        command += ["-k", filter]
    returncode, stdout, stderr, duration = _run(command, root, timeout)
    parsed = diagnostics.parse_pytest_output(stdout, stderr)
    passed = returncode == 0
    return {
        "tool": "pytest",
        "command": command,
        "status": "PASSED" if passed else "FAILED",
        "exit_code": returncode,
        "duration_seconds": round(duration, 3),
        "counts": parsed["counts"],
        "failures": parsed["failures"],
        "log_tail": None if passed else parsed["log_tail"],
    }


def run_lint(root: Path, timeout: int = 300) -> dict[str, Any]:
    """Run whichever linter is available for the detected language(s).

    Currently wired: `ruff check` for Python. `clang-tidy` is not run here
    as a whole-project sweep -- it needs a `compile_commands.json` and a
    specific file list, and a single timeout budget across an entire C++
    project is the wrong shape for that; use a per-file `clang-tidy`
    invocation (or `semantic_backends`' clang path) for C++ until a
    project-wide runner is worth adding.
    """
    info = detect_toolchain(root)
    python = info["toolchains"].get("python")
    if python and python["executables"].get("ruff"):
        ruff = python["executables"]["ruff"]
        command = [ruff, "check", "--output-format=concise", "."]
        returncode, stdout, stderr, duration = _run(command, root, timeout)
        issues = diagnostics.parse_ruff_check_output(stdout)
        # ruff check exits 1 for "issues found", >1 for a real tool failure.
        tool_failed = returncode not in (0, 1)
        return {
            "tool": "ruff check",
            "command": command,
            "status": "FAILED" if tool_failed else ("ISSUES_FOUND" if issues else "PASSED"),
            "exit_code": returncode,
            "duration_seconds": round(duration, 3),
            "issue_count": len(issues),
            "issues": issues,
            "log_tail": diagnostics.log_tail(f"{stdout}\n{stderr}") if tool_failed else None,
        }
    raise UnsupportedLanguageError(f"no supported linter detected under {root} (checked: ruff)")


def run_format(root: Path, check_only: bool = True, timeout: int = 120) -> dict[str, Any]:
    """Format (or check formatting of) the project with whatever formatter is available.

    `check_only=True` (the default) never modifies a file -- it reports
    which files would change. Pass `check_only=False` to actually rewrite
    them via the same tool.
    """
    info = detect_toolchain(root)
    python = info["toolchains"].get("python")
    if python and python["executables"].get("ruff"):
        ruff = python["executables"]["ruff"]
        command = [ruff, "format"]
        command += ["--check", "--diff"] if check_only else []
        command += ["."]
        returncode, stdout, stderr, duration = _run(command, root, timeout)
        # `ruff format --check` exits 1 when files would be reformatted.
        tool_failed = returncode not in (0, 1)
        would_reformat = stdout.count("Would reformat") if check_only else None
        return {
            "tool": "ruff format",
            "command": command,
            "check_only": check_only,
            "status": (
                "FAILED"
                if tool_failed
                else "WOULD_REFORMAT"
                if (check_only and returncode == 1)
                else "PASSED"
            ),
            "exit_code": returncode,
            "duration_seconds": round(duration, 3),
            "files_would_reformat": would_reformat,
            "log_tail": diagnostics.log_tail(f"{stdout}\n{stderr}") if tool_failed else None,
        }
    cpp = info["toolchains"].get("cmake")
    if cpp and info["semantic_tools"].get("clang-format"):
        raise UnsupportedLanguageError(
            "clang-format is available but whole-project formatting needs an explicit file "
            "list to avoid touching vendored/generated files -- not run automatically"
        )
    raise UnsupportedLanguageError(f"no supported formatter detected under {root} (checked: ruff)")


_RUFF_FIXED = re.compile(r"^Fixed (?P<n>\d+) errors?", re.MULTILINE)


def run_import_fix(
    root: Path, path_prefix: str | None = None, select: str = "I,F401", timeout: int = 120
) -> dict[str, Any]:
    """Sort imports and/or drop unused ones, via `ruff check --select <select> --fix`.

    Python only for now: `select="I,F401"` (ruff's isort + pyflakes
    unused-import fixers) is "organize imports"; `select="F401"` is just
    "remove unused imports". Rewrites files in place; the caller
    (`core/context/refactor`) wraps this in a snapshot/validate/rollback
    transaction.
    """
    info = detect_toolchain(root)
    python = info["toolchains"].get("python")
    if not (python and python["executables"].get("ruff")):
        raise UnsupportedLanguageError(
            f"organize_imports needs ruff on PATH (Python only for now); none found under {root}"
        )
    ruff = python["executables"]["ruff"]
    target = path_prefix or "."
    command = [ruff, "check", "--select", select, "--fix", "--output-format", "concise", target]
    returncode, stdout, stderr, duration = _run(command, root, timeout)
    combined = f"{stdout}\n{stderr}"
    match = _RUFF_FIXED.search(combined)
    tool_failed = returncode not in (0, 1)
    return {
        "tool": "ruff --fix I,F401",
        "command": command,
        "status": "FAILED" if tool_failed else "OK",
        "exit_code": returncode,
        "duration_seconds": round(duration, 3),
        "fixed": int(match.group("n")) if match else 0,
        "remaining_issues": returncode == 1,
        "log_tail": diagnostics.log_tail(combined) if tool_failed else None,
    }


def run_ruff_fix(
    root: Path, paths: list[str], do_format: bool = True, timeout: int = 120
) -> dict[str, Any]:
    """Apply ruff's safe autofixes (and optionally `ruff format`) to a specific file list.

    Used by the local repair loop: an edit that introduced an unused
    import, a stale `f`-string, or a formatting drift is exactly the kind
    of deterministic fix-up that should never round-trip to the agent.
    """
    info = detect_toolchain(root)
    python = info["toolchains"].get("python")
    if not (python and python["executables"].get("ruff")):
        raise UnsupportedLanguageError(f"ruff is not available under {root}")
    ruff = python["executables"]["ruff"]
    targets = [p for p in paths if p.endswith(".py")]
    if not targets:
        return {"tool": "ruff", "fixed": 0, "formatted": 0}

    fix = _run([ruff, "check", "--fix", "--output-format", "concise", *targets], root, timeout)
    fixed = int(m.group("n")) if (m := _RUFF_FIXED.search(f"{fix[1]}\n{fix[2]}")) else 0
    formatted = 0
    if do_format:
        fmt = _run([ruff, "format", *targets], root, timeout)
        formatted = fmt[1].count("file reformatted") + fmt[1].count("files reformatted")
    return {"tool": "ruff --fix + format", "fixed": fixed, "formatted": formatted}


__all__ = [
    "run_build",
    "run_tests",
    "run_lint",
    "run_format",
    "run_import_fix",
    "run_ruff_fix",
    "DEFAULT_TIMEOUT",
]
