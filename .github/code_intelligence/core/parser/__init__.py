"""Language adapters: turn one source file's text into a `FileAnalysis`.

Ported from `tools/code_quality/code_quality/adapters/`.
"""

from code_intelligence.core.parser.base import LanguageAdapter
from code_intelligence.core.parser.cpp_adapter import CppAdapter
from code_intelligence.core.parser.java_adapter import JavaAdapter
from code_intelligence.core.parser.javascript_adapter import JavaScriptAdapter
from code_intelligence.core.parser.php_adapter import PhpAdapter
from code_intelligence.core.parser.python_adapter import PythonAdapter

#: Every adapter, in the same probe order the old tool used.
ADAPTERS: list[LanguageAdapter] = [
    PythonAdapter(),
    CppAdapter(),
    JavaAdapter(),
    PhpAdapter(),
    JavaScriptAdapter(),
]


def find_adapter(path) -> LanguageAdapter | None:
    """Return the first adapter that claims this file's extension."""
    for adapter in ADAPTERS:
        if adapter.can_handle(path):
            return adapter
    return None


__all__ = [
    "LanguageAdapter",
    "PythonAdapter",
    "CppAdapter",
    "JavaAdapter",
    "PhpAdapter",
    "JavaScriptAdapter",
    "ADAPTERS",
    "find_adapter",
]
