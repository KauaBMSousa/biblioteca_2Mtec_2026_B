"""Semantic backends: exact answers from per-language toolchains.

tree-sitter parses; it does not type-check. The questions that need types --
references, rename, extract -- belong to the language's own engine, and
every answer here says which engine produced it.

    cpp         libclang            compile_commands.json
    python      jedi + LibCST       the source tree
    javascript  TypeScript API      tsconfig.json (optional)
    java        javac tree API      a JDK and .java sources
    php         nikic/php-parser    composer

`refresh` keeps the extracted edges in step with the code: a unit is
re-extracted when its own text changes AND when anything it read changes,
because exact data that has silently gone stale is worse than heuristic
data that admits what it is.
"""

from code_intelligence.core.semantic.backends import (
    CONFIDENCE_EXACT,
    CONFIDENCE_HEURISTIC,
    clang_references,
    semantic_backends,
)
from code_intelligence.core.semantic.protocol import ExtractionResult, SemanticBackend, SemanticEdge
from code_intelligence.core.semantic.refresh import refresh
from code_intelligence.core.semantic.registry import BACKENDS, availability_report, backend_for

__all__ = [
    "BACKENDS",
    "CONFIDENCE_EXACT",
    "CONFIDENCE_HEURISTIC",
    "ExtractionResult",
    "SemanticBackend",
    "SemanticEdge",
    "availability_report",
    "backend_for",
    "clang_references",
    "refresh",
    "semantic_backends",
]
