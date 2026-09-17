"""Detect which build/test/lint/format toolchain applies to a project, and
which of the executables it would need are actually on PATH.

Mirrors `core.semantic.backends`' "report availability, never guess" rule:
a missing tool must surface as an explicit `False`/`None`, never as a
silent skip that a caller could misread as "nothing to build here". A
project can declare more than one toolchain (a C++ core with a Python test
harness, say) -- every marker file present is reported, not just the
first match, so `run_tests` can raise a clear "which one?" error instead
of guessing.
"""

import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

#: Marker file -> toolchain name, in the order checked. A project can match
#: more than one; each match is reported independently.
_CMAKE_MARKERS = ("CMakeLists.txt",)
_PYTHON_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")
_NODE_MARKERS = ("package.json",)
_MAVEN_MARKERS = ("pom.xml",)
_GRADLE_MARKERS = ("build.gradle", "build.gradle.kts")
_COMPOSER_MARKERS = ("composer.json",)


def _which(*names: str) -> dict[str, str | None]:
    return {name: shutil.which(name) for name in names}


def _first_present(root: Path, markers: tuple[str, ...]) -> str | None:
    for marker in markers:
        if (root / marker).exists():
            return marker
    return None


def _python_interpreter(root: Path) -> str:
    """Prefer the project's own virtualenv interpreter over the ambient one.

    A project-local `.venv` is where its actual pytest/ruff versions live;
    falling back to whatever `python3` resolves to on PATH would silently
    run the wrong dependency set (or none at all) for a project that pins
    its own.
    """
    for candidate in (".venv/bin/python", "venv/bin/python", ".venv/Scripts/python.exe"):
        path = root / candidate
        if path.exists():
            return str(path)
    return sys.executable


def _python_module_available(interpreter: str, module: str, timeout: float = 10.0) -> bool:
    try:
        probe = subprocess.run(
            [interpreter, "-c", f"import {module}"],
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return probe.returncode == 0


def detect_toolchain(root: Path) -> dict[str, Any]:
    """What this project builds/tests/lints/formats with, and what's installed.

    Returns one entry per detected toolchain under `toolchains`, each with
    an `available` bool (its primary tool is actually runnable) and an
    `executables` map (every relevant tool this toolchain could use, each
    either its resolved path or `None`). `general_tools` and
    `semantic_tools` report ecosystem-wide tools useful regardless of
    which toolchain applies (git, ripgrep, fd; clangd/clang-tidy/
    clang-format for any C++ present).
    """
    toolchains: dict[str, dict[str, Any]] = {}

    cmake_marker = _first_present(root, _CMAKE_MARKERS)
    if cmake_marker:
        exes = _which("cmake", "ctest", "ninja", "make")
        toolchains["cmake"] = {
            "marker": cmake_marker,
            "build_tool": "cmake",
            "test_tool": "ctest",
            "available": exes["cmake"] is not None and exes["ctest"] is not None,
            "executables": exes,
        }

    python_marker = _first_present(root, _PYTHON_MARKERS)
    if python_marker:
        interpreter = _python_interpreter(root)
        exes = _which("pytest", "ruff", "mypy", "black")
        has_pytest = exes["pytest"] is not None or _python_module_available(interpreter, "pytest")
        toolchains["python"] = {
            "marker": python_marker,
            "interpreter": interpreter,
            "test_tool": "pytest",
            "lint_tool": "ruff" if exes["ruff"] else None,
            "format_tool": "ruff" if exes["ruff"] else ("black" if exes["black"] else None),
            "available": has_pytest,
            "executables": exes,
        }

    node_marker = _first_present(root, _NODE_MARKERS)
    if node_marker:
        exes = _which("npm", "node", "yarn", "pnpm", "eslint", "prettier")
        toolchains["node"] = {
            "marker": node_marker,
            "available": exes["node"] is not None and exes["npm"] is not None,
            "executables": exes,
        }

    maven_marker = _first_present(root, _MAVEN_MARKERS)
    if maven_marker:
        exes = _which("mvn", "java")
        toolchains["maven"] = {
            "marker": maven_marker,
            "available": exes["mvn"] is not None,
            "executables": exes,
        }

    gradle_marker = _first_present(root, _GRADLE_MARKERS)
    if gradle_marker:
        exes = _which("gradle", "java")
        toolchains["gradle"] = {
            "marker": gradle_marker,
            "available": exes["gradle"] is not None,
            "executables": exes,
        }

    composer_marker = _first_present(root, _COMPOSER_MARKERS)
    if composer_marker:
        exes = _which("composer", "php", "phpunit")
        toolchains["composer"] = {
            "marker": composer_marker,
            "available": exes["composer"] is not None,
            "executables": exes,
        }

    return {
        "root": str(root),
        "toolchains": toolchains,
        "general_tools": _which("git", "rg", "fd"),
        "semantic_tools": _which("clangd", "clang-tidy", "clang-format"),
        "analysis_tools": _which("cppcheck"),
    }


__all__ = ["detect_toolchain"]
