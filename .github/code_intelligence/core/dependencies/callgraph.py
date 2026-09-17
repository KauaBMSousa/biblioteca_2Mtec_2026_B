"""Project-wide, name-matching call-graph resolution.

Runs once after every file in the run has been analyzed and had its
`symbol_id`s assigned. Builds one `name -> [symbol_id, ...]` index across
every `FunctionInfo`/`ClassInfo` in the run, then for each function scans
its already-collected `identifiers` (kind `"function"` / `"class"` —
call/instantiation-like references) and resolves each name against the
index.

Confidence is capped at `"medium"` (same-file, unique match) or `"low"`
(cross-file, or ambiguous/multi-candidate match) — never `"high"`, since
this stays a heuristic name-matching pass regardless of resolution scope.
This mutates `FunctionInfo` in place.

Ported unchanged (besides import paths) from
`tools/code_quality/code_quality/callgraph.py`. `core/index/indexer.py`
persists the resolved `calls`/`called_by`/`references` edges into the
`dependencies` table instead of recomputing them in memory on every run.
"""

from code_intelligence.core.parser.ir import FileAnalysis, FunctionInfo

FileResult = tuple[FileAnalysis, list]

#: Identifier kinds that plausibly represent a call/instantiation reference,
#: as opposed to a variable/parameter/loop-index declaration.
_CALL_LIKE_KINDS = {"function", "class"}


def _build_name_index(results: list[FileResult]) -> dict[str, list[tuple[str, str]]]:
    """Build `name -> [(symbol_id, relpath), ...]` across every analyzed file."""
    index: dict[str, list[tuple[str, str]]] = {}

    def register(name: str, symbol_id: str, relpath: str) -> None:
        if not symbol_id:
            return
        index.setdefault(name, []).append((symbol_id, relpath))
        # A C++ method is stored as `Owner::method` and a Python one as
        # `Owner.method`, but the CALL SITE writes `obj.method()`, whose
        # identifier is the bare `method`. Indexing only the stored name
        # meant every method call through an object failed to resolve, and
        # `find_references` answered "0 callers" for a method that plainly
        # had one -- a silent wrong answer, and a dangerous one: for an
        # agent deciding what is safe to change, "nothing calls this" reads
        # as "dead code".
        #
        # A bare name is more ambiguous, which the resolver already handles:
        # several candidates downgrade the edge from a `call` to a
        # `reference` with lower confidence, rather than guessing which
        # overload was meant.
        tail = name.split("::")[-1].split(".")[-1]
        if tail and tail != name:
            # Keyed BY LANGUAGE. A bare `run` is common enough that an
            # unqualified tail index matched a Python test's `run_rank`
            # against a C++ `AutoencoderRunner::run` -- trading a silent
            # false negative for confident-looking noise, which is worse.
            language = symbol_id.split(":", 1)[0]
            index.setdefault(f"{language}\x00{tail}", []).append((symbol_id, relpath))

    for analysis, _violations in results:
        for cls in analysis.classes:
            register(cls.name, cls.symbol_id, analysis.relpath)
            for method in cls.methods:
                register(method.name, method.symbol_id, analysis.relpath)
        for func in analysis.top_level_functions:
            register(func.name, func.symbol_id, analysis.relpath)

    return index


def _resolve_one(
    name: str,
    own_relpath: str,
    index: dict[str, list[tuple[str, str]]],
    language: str = "",
) -> tuple[list[str], list[str], str | None]:
    """Resolve one referenced name against the project-wide index.

    Returns:
        A tuple `(calls, references, confidence)`. `calls` is populated only
        for an unresolvable-to-single-candidate match (same-file or
        cross-file); `references` is populated instead when multiple
        candidates exist (which one is actually invoked is genuinely
        unknown). `confidence` is None when there was no match at all.
    """
    candidates = list(index.get(name) or [])
    if language:
        # A method invoked through an object (`obj.method()`) is recorded by
        # its bare name, while the symbol is stored qualified (`Owner::method`
        # in C++, `Owner.method` in Python), so the qualified candidates are
        # merged in here rather than consulted only when the bare lookup
        # comes up empty.
        #
        # Merging, not falling back: a bare `run()` can mean any `X::run` in
        # the language, and one of them happening to ALSO be named plainly
        # `run` does not make it the answer. That is what used to happen --
        # the exact-name lookup found a single unrelated `run`, resolved to
        # it with confidence, and the real callee was never considered.
        # Several candidates is the truth here, and the code below already
        # reports that as a low-confidence `reference` instead of a call.
        candidates += index.get(f"{language}\x00{name}") or []
    if language:
        # A call in one language can never resolve to a symbol in another.
        # Without this filter a C++ `experiment.run()` matched every Python
        # `def run(...)` in the tree -- 10495 of the call graph's edges were
        # cross-language matches of a common short name, reported with the
        # same confidence as a real one.
        candidates = [
            (symbol_id, relpath)
            for symbol_id, relpath in (candidates or [])
            if symbol_id.startswith(f"{language}:")
        ]
        candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return [], [], None

    same_file = [symbol_id for symbol_id, relpath in candidates if relpath == own_relpath]
    if len(same_file) == 1:
        return [same_file[0]], [], "medium"

    all_ids = [symbol_id for symbol_id, _relpath in candidates]
    if len(all_ids) == 1:
        return [all_ids[0]], [], "low"

    # Multiple candidates (same-file ambiguous, or cross-file with >1 hit):
    # which one is actually invoked is genuinely unknown.
    return [], all_ids, "low"


def _referenced_names(func: FunctionInfo) -> set[str]:
    """The set of call/instantiation-like names referenced within `func`."""
    return {ident.name for ident in func.identifiers if ident.kind in _CALL_LIKE_KINDS}


def _resolve_function(
    func: FunctionInfo,
    own_relpath: str,
    index: dict[str, list[tuple[str, str]]],
    language: str = "",
) -> None:
    """Populate `func.calls`/`func.references`/`func.call_graph_confidence` in place."""
    calls: list[str] = []
    references: list[str] = []
    confidences: list[str] = []

    for name in sorted(_referenced_names(func)):
        resolved_calls, resolved_refs, confidence = _resolve_one(
            name, own_relpath, index, language
        )
        calls.extend(resolved_calls)
        references.extend(resolved_refs)
        if confidence is not None:
            confidences.append(confidence)

    func.calls = calls
    func.references = references
    # "low" if any resolution was low-confidence, else "medium" (default kept
    # when there were no resolvable references at all).
    func.call_graph_confidence = "low" if "low" in confidences else "medium"


def _all_functions(analysis: FileAnalysis) -> list[FunctionInfo]:
    """Every function/method in one file, top-level and class-owned."""
    functions = list(analysis.top_level_functions)
    for cls in analysis.classes:
        functions.extend(cls.methods)
    return functions


def _invert_called_by(results: list[FileResult]) -> None:
    """Build `called_by` as the inverse of every function's resolved `calls`."""
    by_symbol_id: dict[str, FunctionInfo] = {}
    for analysis, _violations in results:
        for func in _all_functions(analysis):
            if func.symbol_id:
                by_symbol_id[func.symbol_id] = func

    for analysis, _violations in results:
        for func in _all_functions(analysis):
            for callee_id in func.calls:
                callee = by_symbol_id.get(callee_id)
                if callee is not None and func.symbol_id not in callee.called_by:
                    callee.called_by.append(func.symbol_id)


def resolve(results: list[FileResult]) -> None:
    """Resolve call-graph hints for every function across `results`, in place.

    Must run after every file has been analyzed and had `symbol_id`
    assigned (project-wide resolution needs the full-run symbol index).
    """
    index = _build_name_index(results)
    for analysis, _violations in results:
        for func in _all_functions(analysis):
            _resolve_function(func, analysis.relpath, index, analysis.language)
    _invert_called_by(results)


__all__ = ["resolve"]
