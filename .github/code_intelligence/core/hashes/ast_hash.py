"""Structural (AST-level) hashing, distinct from `content_hash`'s byte hashing.

`content_hash` (blake2b `file_hash` in `FileAnalysis`, or the sha256
`content_hash` on a `SymbolRecord`) changes on *any* byte edit, including
ones that don't touch the file's extracted structure at all (renaming a
local variable, editing a string literal, reformatting a comment).
`ast_hash` changes only when the file's *structural* symbol data itself
changes — new/removed/renamed symbols, a different parameter count, a
different nesting/complexity shape, a doc comment appearing/disappearing.

This is what lets `core/index/indexer.py` distinguish "this file's bytes
changed but nothing downstream (dependents, duplication groups) needs to be
recomputed" from a real structural change, without a separate cache
implementation (the index itself stores both hashes per file, per
`core/cache/`'s "the index IS the cache" design).
"""

import hashlib
import json

from code_intelligence.core.parser.ir import FileAnalysis, FunctionInfo


def _function_signature(func: FunctionInfo) -> dict:
    """The structural (not positional, not textual) facts about one function."""
    param_count = sum(1 for ident in func.identifiers if ident.kind == "parameter")
    return {
        "kind": "method" if func.is_method else "function",
        "name": func.name,
        "qualified_name": func.qualified_name,
        "owner_class": func.owner_class,
        "namespace": func.namespace,
        "param_count": param_count,
        "nesting_depth": func.nesting_depth,
        "cyclomatic_complexity": func.cyclomatic_complexity,
        "has_doc": func.has_doc,
    }


def _class_signature(cls) -> dict:
    """The structural facts about one class/struct/interface/enum/..., including its methods."""
    return {
        "kind": cls.kind,
        "name": cls.name,
        "namespace": cls.namespace,
        "has_doc": cls.has_doc,
        "methods": [_function_signature(method) for method in cls.methods],
    }


def compute_ast_hash(analysis: FileAnalysis) -> str:
    """Compute a canonical, position-independent structural hash of one file.

    Args:
        analysis: A fully parsed `FileAnalysis` (classes/top_level_functions
            populated). Works equally on a parse-fallback result (an empty
            structural list still hashes deterministically to the same
            value for two equally-unparseable files).

    Returns:
        A ``"sha256:" + hexdigest"`` string, independent of line/column
        positions, doc-comment/variable/literal text, or `content_hash`.
    """
    payload = {
        "language": analysis.language,
        "classes": [_class_signature(cls) for cls in analysis.classes],
        "top_level_functions": [_function_signature(func) for func in analysis.top_level_functions],
        "parse_ok": analysis.parse_ok,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


__all__ = ["compute_ast_hash"]
