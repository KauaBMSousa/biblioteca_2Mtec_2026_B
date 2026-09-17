"""Build/test/lint/format orchestration: delegate to the project's own toolchain.

Mirrors `core.semantic`'s rule: report what is available rather than
guessing, and never silently do nothing. A failing build or test run is a
normal, expected outcome (`status: "FAILED"`, with structured diagnostics);
these modules raise only for infrastructure problems -- no usable tool
found, an unsupported project layout, or a timeout.
"""

from code_intelligence.core.exec.errors import ToolNotInstalledError, UnsupportedLanguageError
from code_intelligence.core.exec.runner import (
    run_build,
    run_format,
    run_import_fix,
    run_lint,
    run_ruff_fix,
    run_tests,
)
from code_intelligence.core.exec.cluster import cluster_failures
from code_intelligence.core.exec.toolchain import detect_toolchain

__all__ = [
    "ToolNotInstalledError",
    "UnsupportedLanguageError",
    "detect_toolchain",
    "run_build",
    "run_tests",
    "run_lint",
    "run_format",
    "run_import_fix",
    "run_ruff_fix",
    "cluster_failures",
]
