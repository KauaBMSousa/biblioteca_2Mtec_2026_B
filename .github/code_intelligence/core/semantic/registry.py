"""The five semantic backends, and which one serves a given language.

Registration is explicit rather than discovered: a backend that silently
fails to load would leave its language answered by name matching while
still looking like it had exact data, and that is the confusion this whole
subsystem exists to remove.
"""

from pathlib import Path
from typing import Any

from code_intelligence.core.semantic import cpp_clang, java_rewrite, js_tsmorph, php_parser, py_jedi
from code_intelligence.core.semantic.protocol import SemanticBackend

#: language -> backend instance. One per language the index can parse.
BACKENDS: dict[str, SemanticBackend] = {
    "cpp": cpp_clang.build(),
    "python": py_jedi.build(),
    "javascript": js_tsmorph.build(),
    "java": java_rewrite.build(),
    "php": php_parser.build(),
}


def backend_for(language: str) -> SemanticBackend | None:
    """The backend serving `language`, or None when there is none."""
    return BACKENDS.get(language)


def availability_report(root: Path) -> dict[str, Any]:
    """Per-language: which tool, whether it can run here, and what is missing."""
    report: dict[str, Any] = {}
    for language, backend in BACKENDS.items():
        available, missing = backend.availability(root)
        report[language] = {
            "tool": backend.tool,
            "available": available,
            "missing": missing,
        }
    return report
