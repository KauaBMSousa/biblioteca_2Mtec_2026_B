"""Deterministic symbol_id computation and per-file symbol flattening.

Ported from `tools/code_quality/code_quality/symbols.py`. Symbol IDs follow
the spec format ``{language}:{relpath}:{kind}:{qualified_name}``, with two
content-derived (never position-derived) tie-breakers for legitimate
same-name overloads (C++/Java allow same-name-different-signature methods):

1. When >=2 params-bearing symbols in the same file share a
   ``qualified_name``, append ``/{param_count}`` to the qualified name
   portion of the ID.
2. If that still collides (same name, same arg count, different types),
   append a short ``#{sha256(normalized_param_text)[:8]}`` suffix.
3. If THAT still collides, re-key the colliding set with
   ``#{sha256(token_stream)[:8]}`` instead. Tie-breaker 2 sees only
   parameter *names*, and C++ routinely omits them -- ``= delete``d
   copy/move pairs and const/non-const overload pairs both hash to
   sha256("") and collide, which used to abort the whole index pass on
   the ``symbols.symbol_id`` UNIQUE constraint.

Both tie-breakers are derived from the symbol's own signature, not its
position, so IDs stay stable across pure line-shifting refactors and only
change when the symbol's own signature changes. This is load-bearing per
fixme.md's explicit "não utilizar exclusivamente ranges ou linhas" —
symbol_id must never be range/line-based alone.
"""

import hashlib

from code_intelligence.core.hashes.content_hash import content_hash
from code_intelligence.core.parser.ir import ClassInfo, FileAnalysis, FunctionInfo
from code_intelligence.core.symbols.records import SymbolRecord

#: Function kinds that get a "constructor" SymbolRecord kind instead of
#: "method"/"function", based on the enclosing class's language convention.
_CONSTRUCTOR_NAMES = {"__init__"}


def _param_count(func: FunctionInfo) -> int:
    """Approximate a function's parameter count from its `parameter`-kind identifiers."""
    return sum(1 for ident in func.identifiers if ident.kind == "parameter")


def _normalized_param_text(func: FunctionInfo) -> str:
    """Build a stable, content-derived text of a function's parameter names."""
    names = sorted(ident.name for ident in func.identifiers if ident.kind == "parameter")
    return ",".join(names)


def _signature_token_digest(func: FunctionInfo) -> str:
    """Short hash of a function's own token stream.

    The last-resort tie-breaker, for symbols whose parameter *names* are
    identical because the language does not require naming them. C++ hits
    this constantly, in two idioms:

        T* operator->();                 // 0 params, no names
        const T* operator->() const;     // 0 params, no names -> same digest

        X(const X&) = delete;            // 1 unnamed param
        X(X&&)      = delete;            // 1 unnamed param -> same digest

    `_normalized_param_text` returns "" for both members of each pair, so
    both hash to sha256("") and collide. The token stream does not: it
    carries the types, the const-qualification and the `= delete`, so the
    two members differ. It is still content-derived, not position-derived,
    so IDs stay stable across pure line-shifting refactors.
    """
    return hashlib.sha256("\n".join(str(t) for t in (func.token_stream or ())).encode("utf-8")).hexdigest()[:8]


def _base_symbol_id(language: str, relpath: str, kind: str, qualified_name: str) -> str:
    """Build the un-disambiguated `{language}:{relpath}:{kind}:{qualified_name}` ID."""
    return f"{language}:{relpath}:{kind}:{qualified_name}"


def _function_kind(func: FunctionInfo) -> str:
    """Return the SymbolRecord kind for a FunctionInfo.

    Honors an adapter-assigned `func.kind` (e.g. "constructor",
    "destructor", "arrow_function") when it's been set to something other
    than the generic default; otherwise falls back to the original
    method/constructor/function inference.
    """
    if func.kind not in ("function", ""):
        return func.kind
    if func.is_method and func.name in _CONSTRUCTOR_NAMES:
        return "constructor"
    return "method" if func.is_method else "function"


def compute_function_symbol_ids(language: str, relpath: str, functions: list[FunctionInfo]) -> None:
    """Assign `symbol_id` to every FunctionInfo in `functions`, in place.

    Disambiguates legitimate same-name overloads by param_count, then by a
    short hash of normalized parameter text, both content-derived.
    """
    by_qualified: dict[str, list[FunctionInfo]] = {}
    for func in functions:
        by_qualified.setdefault(func.qualified_name, []).append(func)

    for qualified_name, group in by_qualified.items():
        kind = _function_kind(group[0])
        if len(group) == 1:
            group[0].symbol_id = _base_symbol_id(language, relpath, kind, qualified_name)
            continue

        # >=2 symbols sharing a qualified_name: disambiguate by param_count first.
        by_param_count: dict[int, list[FunctionInfo]] = {}
        for func in group:
            by_param_count.setdefault(_param_count(func), []).append(func)

        for param_count, subgroup in by_param_count.items():
            qname_with_count = f"{qualified_name}/{param_count}"
            if len(subgroup) == 1:
                kind = _function_kind(subgroup[0])
                subgroup[0].symbol_id = _base_symbol_id(language, relpath, kind, qname_with_count)
                continue
            # Still colliding: same name, same param count, different signatures.
            for func in subgroup:
                kind = _function_kind(func)
                digest = hashlib.sha256(_normalized_param_text(func).encode("utf-8")).hexdigest()[:8]
                func.symbol_id = _base_symbol_id(
                    language, relpath, kind, f"{qname_with_count}#{digest}"
                )
            _disambiguate_by_tokens(language, relpath, subgroup, qname_with_count)


def _disambiguate_by_tokens(
    language: str, relpath: str, subgroup: list[FunctionInfo], qname_with_count: str
) -> None:
    """Re-key any symbols in `subgroup` that still share an ID, by token stream.

    Runs after the parameter-name digest has been assigned. Symbols that
    came out unique are left exactly as they were, so this never changes an
    ID that was already fine; only a colliding set is re-keyed, and it is
    re-keyed with `_signature_token_digest`.

    Without this the index simply cannot be built for a C++ codebase that
    uses `= delete`d copy/move pairs or const/non-const overload pairs:
    `INSERT INTO symbols` fails on the UNIQUE constraint and the whole
    pass aborts. Two functions with byte-identical token streams are a
    genuine duplicate declaration; they get an ordinal so the insert still
    succeeds rather than taking the index down.
    """
    by_id: dict[str, list[FunctionInfo]] = {}
    for func in subgroup:
        by_id.setdefault(func.symbol_id, []).append(func)

    for colliding in by_id.values():
        if len(colliding) == 1:
            continue
        for func in colliding:
            kind = _function_kind(func)
            func.symbol_id = _base_symbol_id(
                language, relpath, kind, f"{qname_with_count}#{_signature_token_digest(func)}"
            )
        # Byte-identical token streams: a real duplicate. Ordinal, in parse
        # order, purely so the ID stays unique.
        seen: dict[str, int] = {}
        for func in colliding:
            count = seen.get(func.symbol_id, 0)
            seen[func.symbol_id] = count + 1
            if count:
                func.symbol_id = f"{func.symbol_id}~{count}"


def compute_class_symbol_ids(language: str, relpath: str, classes: list[ClassInfo]) -> None:
    """Assign `symbol_id` to every ClassInfo in `classes`, in place.

    Types don't legitimately overload by signature the way functions do,
    but a same-name collision (rare, e.g. conditional compilation branches)
    still gets an index suffix so IDs stay unique.
    """
    by_name: dict[str, list[ClassInfo]] = {}
    for cls in classes:
        by_name.setdefault(cls.name, []).append(cls)

    for name, group in by_name.items():
        for index, cls in enumerate(group):
            qualified = name if len(group) == 1 else f"{name}#{index}"
            cls.symbol_id = _base_symbol_id(language, relpath, cls.kind, qualified)


def _all_functions(analysis: FileAnalysis) -> list[FunctionInfo]:
    """Every function/method in one file, top-level and class-owned."""
    functions = list(analysis.top_level_functions)
    for cls in analysis.classes:
        functions.extend(cls.methods)
    return functions


def assign_symbol_ids(analysis: FileAnalysis) -> None:
    """Compute and assign `symbol_id` on every ClassInfo/FunctionInfo in `analysis`, in place."""
    compute_class_symbol_ids(analysis.language, analysis.relpath, analysis.classes)
    compute_function_symbol_ids(analysis.language, analysis.relpath, _all_functions(analysis))


def _function_source_span(source_lines: list[str], func: FunctionInfo) -> str:
    """Extract a function's exact source text span for content hashing."""
    return "\n".join(source_lines[func.start_line - 1 : func.end_line])


def _class_source_span(source_lines: list[str], cls: ClassInfo) -> str:
    """Extract a class's exact source text span for content hashing."""
    return "\n".join(source_lines[cls.start_line - 1 : cls.end_line])


def _function_record(analysis: FileAnalysis, func: FunctionInfo, source_lines: list[str] | None) -> SymbolRecord:
    """Build one SymbolRecord for a function/method."""
    span = _function_source_span(source_lines, func) if source_lines is not None else ""
    return SymbolRecord(
        kind=_function_kind(func),
        name=func.name,
        qualified_name=func.qualified_name,
        symbol_id=func.symbol_id,
        file=analysis.relpath,
        language=analysis.language,
        start_line=func.start_line,
        end_line=func.end_line,
        start_column=func.start_column,
        end_column=func.end_column,
        body_start_line=func.body_start_line or func.start_line,
        body_end_line=func.body_end_line or func.end_line,
        loc=func.physical_lines,
        cyclomatic_complexity=func.cyclomatic_complexity,
        has_doc=func.has_doc,
        namespace=func.namespace,
        calls=list(func.calls),
        called_by=list(func.called_by),
        references=list(func.references),
        call_graph_confidence=func.call_graph_confidence,
        content_hash=content_hash(span) if source_lines is not None else "",
        confidence=func.kind_confidence,
    )


def _class_record(analysis: FileAnalysis, cls: ClassInfo, source_lines: list[str] | None) -> SymbolRecord:
    """Build one SymbolRecord for a class/struct/interface/enum/..."""
    span = _class_source_span(source_lines, cls) if source_lines is not None else ""
    return SymbolRecord(
        kind=cls.kind,
        name=cls.name,
        qualified_name=cls.name,
        symbol_id=cls.symbol_id,
        file=analysis.relpath,
        language=analysis.language,
        start_line=cls.start_line,
        end_line=cls.end_line,
        start_column=cls.start_column,
        end_column=cls.end_column,
        body_start_line=cls.start_line,
        body_end_line=cls.end_line,
        loc=cls.end_line - cls.start_line + 1,
        cyclomatic_complexity=None,
        has_doc=cls.has_doc,
        namespace=cls.namespace,
        content_hash=content_hash(span) if source_lines is not None else "",
        confidence=cls.kind_confidence,
    )


def find_symbol_id_for_line(analysis: FileAnalysis, line: int) -> str | None:
    """Find the innermost symbol (method > function > class) whose span contains `line`.

    Shared by `core/index/indexer.py` (attaching `symbol_id` to a freshly
    computed violation) and `core/workspace/workspace.py`'s `diff()` (the
    same lookup against an ad-hoc, not-yet-indexed `FileAnalysis`).
    """
    for cls in analysis.classes:
        for method in cls.methods:
            if method.start_line <= line <= method.end_line:
                return method.symbol_id
    for func in analysis.top_level_functions:
        if func.start_line <= line <= func.end_line:
            return func.symbol_id
    for cls in analysis.classes:
        if cls.start_line <= line <= cls.end_line:
            return cls.symbol_id
    return None


def flatten_symbols(analysis: FileAnalysis, source: str | None = None) -> list[SymbolRecord]:
    """Flatten every class/function/method in `analysis` into SymbolRecords.

    Args:
        analysis: The file's IR, with `symbol_id`s already assigned via
            `assign_symbol_ids`.
        source: The file's full source text, used to compute each symbol's
            `content_hash`. When omitted, `content_hash` is left empty
            (used by callers that only need structural metadata).
    """
    source_lines = source.splitlines() if source is not None else None
    records: list[SymbolRecord] = []
    for cls in analysis.classes:
        records.append(_class_record(analysis, cls, source_lines))
        for method in cls.methods:
            records.append(_function_record(analysis, method, source_lines))
    for func in analysis.top_level_functions:
        records.append(_function_record(analysis, func, source_lines))
    return records


__all__ = [
    "assign_symbol_ids",
    "flatten_symbols",
    "compute_class_symbol_ids",
    "compute_function_symbol_ids",
    "find_symbol_id_for_line",
]
