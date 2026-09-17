"""Cross-file/function duplication detection: DUPLICATE_BLOCK.

Language-agnostic: operates only on each adapter's flat `token_stream`.

Algorithm (as specified in the plan):
  1. Normalize each function's token stream (`IDENT` -> "ID", `LITERAL` ->
     "LIT", `KEYWORD`/`OP` kept verbatim) so structurally identical code
     with renamed variables/different literals still matches.
  2. Skip functions with fewer than `min_tokens` normalized tokens.
  3. Slide a `ngram_window`-token window (stride 1) over each function's
     normalized stream and `blake2b`-hash each window — deterministic, so
     the index stays stable across runs.
  4. Group windows by hash; a hash shared by windows from two *different*
     functions is a candidate match.
  5. For each candidate function pair, merge their overlapping/adjacent
     matching windows into contiguous covered-token regions, then compute
     `similarity = matched_tokens / min(len_a, len_b)`.
  6. Pairs at or above `similarity_threshold` must ALSO share at least
     `content_threshold` of their raw identifiers/literals -- step 1's
     normalization is what catches renamed copies, and also what makes
     every table of same-shaped calls look like one; requiring content too
     separates the two. Pairs clearing both become duplicate edges;
     union-find merges transitively-connected edges into one multi-location
     finding per connected component (not reported pairwise).

Ported unchanged (besides import paths, plus `confidence="medium"` on the
emitted violations — cross-file similarity is a heuristic, never certain)
from `tools/code_quality/code_quality/rules/duplication.py`. The index-
backed incremental wiring (which functions' token streams are available
without a full reparse) lives in `core/index/indexer.py`, which is the
only caller that needs to reconstruct a `list[FileAnalysis]`-shaped input
for `detect()`/`check()` — this module itself is unaware of the index.
"""

import hashlib
from collections import Counter, defaultdict
from itertools import combinations

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.parser.ir import FileAnalysis, FunctionInfo, Token
from code_intelligence.core.rules._union_find import _UnionFind
from code_intelligence.core.rules.duplicate_group import DuplicateGroup

FuncEntry = tuple[FileAnalysis, FunctionInfo]


def _normalize_token(tok: Token) -> str:
    """Normalize one token for duplication matching: IDENT/LITERAL are generalized."""
    kind, text = tok
    if kind == "IDENT":
        return "ID"
    if kind == "LITERAL":
        return "LIT"
    return text


def _body_start_index(token_stream: list[Token]) -> int:
    """Index of the first token past a function's signature, or 0 if none is found.

    The signature (name, parameters, type annotations, return type) is
    boilerplate: two unrelated methods that happen to share a name and
    parameter list -- e.g. `_row_defaults(self, row: int) -> dict[str, str]`
    overridden per subclass to build a different table -- are identical
    there regardless of what the body does, so counting those tokens as
    shared "content" below inflates the similarity of exactly the
    same-shape/different-data case `_content_similarity` exists to exclude.
    Tracks bracket depth from the first `(` so nested type-annotation
    brackets (`dict[str, str]`) don't trip the body-start check early, then
    looks for the `:`/`{` that ends the signature once depth returns to 0.
    Token streams that don't open with a `(` at all (synthetic streams,
    or a body's own first statement happening to be a call) fall back to
    treating the whole stream as body, matching prior behaviour.
    """
    depth = 0
    seen_open_paren = False
    for index, (_kind, text) in enumerate(token_stream):
        if not seen_open_paren:
            if text == "(":
                seen_open_paren = True
                depth = 1
            continue
        if depth == 0 and text in (":", "{"):
            return index + 1
        if text in "([{":
            depth += 1
        elif text in ")]}":
            depth -= 1
    return 0


def _content_bag(func: FunctionInfo) -> "Counter[str]":
    """Multiset of a function's IDENT/LITERAL tokens -- its actual content.

    Excludes the signature (see `_body_start_index`): only the body should
    count as "content" two functions can agree or disagree on.
    """
    body = func.token_stream[_body_start_index(func.token_stream) :]
    return Counter(text for kind, text in body if kind in ("IDENT", "LITERAL"))


def _content_similarity(func_a: FunctionInfo, func_b: FunctionInfo) -> float:
    """How much of two functions' identifiers and literals are shared.

    The counterpart to the shape score. Normalizing IDENT->"ID" and
    LITERAL->"LIT" is what lets this rule catch copy-paste-with-renames,
    but it also makes every TABLE look like a copy: N calls of the same
    shape with different data hash identically, so a config parser
    (`assign(j, "epochs", cfg.training.epochs); assign(j, "k_folds", ...)`)
    scores 1.00 against an unrelated one. Measured over this codebase the
    two populations separate cleanly -- real copies sit at 0.8-1.0 shared
    content, shape-only matches at 0.0-0.5 -- so requiring content as well
    as shape drops the tables without touching the copies.

    Symmetric and normalized by the smaller function, matching how the
    shape score treats a short block repeated inside a long one.
    """
    bag_a, bag_b = _content_bag(func_a), _content_bag(func_b)
    if not bag_a or not bag_b:
        # Nothing but keywords and operators: there is no content to
        # disagree about, so shape is all the evidence there is.
        return 1.0
    shared = sum((bag_a & bag_b).values())
    return shared / min(sum(bag_a.values()), sum(bag_b.values()))


def _hash_window(window: list[str]) -> str:
    """Deterministically hash one token window with blake2b."""
    joined = "\x01".join(window)
    return hashlib.blake2b(joined.encode("utf-8"), digest_size=16).hexdigest()


def _windows(normalized: list[str], window_size: int) -> list[tuple[str, int]]:
    """Return (hash, start_index) for every sliding window of `window_size`."""
    if len(normalized) < window_size:
        return []
    return [
        (_hash_window(normalized[i : i + window_size]), i)
        for i in range(len(normalized) - window_size + 1)
    ]


def _iter_all_functions(analyses: list[FileAnalysis]):
    """Yield (FileAnalysis, FunctionInfo) for every function/method in the fileset."""
    for analysis in analyses:
        for func in analysis.top_level_functions:
            yield analysis, func
        for cls in analysis.classes:
            for method in cls.methods:
                yield analysis, method


def _merge_covered_tokens(positions: list[int], window_size: int) -> int:
    """Merge window start positions into contiguous token ranges; return total covered tokens."""
    if not positions:
        return 0
    positions = sorted(set(positions))
    covered = 0
    range_start = positions[0]
    range_end = positions[0] + window_size
    for start in positions[1:]:
        if start <= range_end:
            range_end = max(range_end, start + window_size)
        else:
            covered += range_end - range_start
            range_start = start
            range_end = start + window_size
    covered += range_end - range_start
    return covered


def _index_windows(
    functions: list[FuncEntry], min_tokens: int, window_size: int
) -> tuple[list[list[str]], list[list[tuple[str, int]]], list[int]]:
    """Normalize every function's tokens and window-hash the ones long enough to consider."""
    normalized_streams: list[list[str]] = []
    windows_per_func: list[list[tuple[str, int]]] = []
    eligible: list[int] = []

    for index, (_analysis, func) in enumerate(functions):
        normalized = [_normalize_token(token) for token in func.token_stream]
        normalized_streams.append(normalized)
        if len(normalized) < min_tokens:
            windows_per_func.append([])
            continue
        windows_per_func.append(_windows(normalized, window_size))
        eligible.append(index)

    return normalized_streams, windows_per_func, eligible


def _find_candidate_pairs(
    windows_per_func: list[list[tuple[str, int]]], eligible: list[int]
) -> set[tuple[int, int]]:
    """Return function-index pairs that share at least one window hash."""
    hash_to_funcs: dict[str, set[int]] = defaultdict(set)
    for index in eligible:
        for digest, _start in windows_per_func[index]:
            hash_to_funcs[digest].add(index)

    candidate_pairs: set[tuple[int, int]] = set()
    for func_indices in hash_to_funcs.values():
        if len(func_indices) < 2:
            continue
        for idx_a, idx_b in combinations(sorted(func_indices), 2):
            candidate_pairs.add((idx_a, idx_b))
    return candidate_pairs


def _score_pairs(
    candidate_pairs: set[tuple[int, int]],
    windows_per_func: list[list[tuple[str, int]]],
    normalized_streams: list[list[str]],
    window_size: int,
    threshold: float,
    functions: list[FuncEntry] | None = None,
    content_threshold: float = 0.0,
) -> tuple[_UnionFind, dict[tuple[int, int], float]]:
    """Score every candidate pair; union-find the pairs that match in BOTH
    shape and content.

    A pair has to clear `threshold` on the normalized (shape) score AND
    `content_threshold` on the raw identifier/literal score. Shape alone
    reports every table, dispatch chain and builder sequence in a codebase
    as duplication -- see `_content_similarity`.
    """
    union_find = _UnionFind()
    pair_similarity: dict[tuple[int, int], float] = {}

    for idx_a, idx_b in candidate_pairs:
        hashes_a = {digest for digest, _ in windows_per_func[idx_a]}
        hashes_b = {digest for digest, _ in windows_per_func[idx_b]}
        shared = hashes_a & hashes_b
        if not shared:
            continue
        positions_a = [start for digest, start in windows_per_func[idx_a] if digest in shared]
        positions_b = [start for digest, start in windows_per_func[idx_b] if digest in shared]
        matched = min(
            _merge_covered_tokens(positions_a, window_size),
            _merge_covered_tokens(positions_b, window_size),
        )
        len_a = len(normalized_streams[idx_a])
        len_b = len(normalized_streams[idx_b])
        similarity = matched / min(len_a, len_b) if min(len_a, len_b) else 0.0
        if similarity < threshold:
            continue
        if functions is not None and content_threshold > 0.0:
            content = _content_similarity(functions[idx_a][1], functions[idx_b][1])
            if content < content_threshold:
                continue  # same shape, different data: a table, not a copy
        union_find.union(idx_a, idx_b)
        pair_similarity[(idx_a, idx_b)] = similarity

    return union_find, pair_similarity


def _build_groups(
    union_find: _UnionFind,
    pair_similarity: dict[tuple[int, int], float],
    functions: list[FuncEntry],
    threshold: float,
) -> list[DuplicateGroup]:
    """Merge pair-similarity edges into one DuplicateGroup per connected component."""
    groups_by_root: dict[int, set[int]] = defaultdict(set)
    for idx_a, idx_b in pair_similarity:
        root = union_find.find(idx_a)
        groups_by_root[root].add(idx_a)
        groups_by_root[root].add(idx_b)

    duplicate_groups: list[DuplicateGroup] = []
    for root, members in groups_by_root.items():
        min_similarity = min(
            (sim for (idx_a, _idx_b), sim in pair_similarity.items() if union_find.find(idx_a) == root),
            default=threshold,
        )
        group = DuplicateGroup(similarity=round(min_similarity, 3))
        for index in sorted(members):
            analysis, func = functions[index]
            group.members.append(
                {
                    "file": analysis.relpath,
                    "function": func.qualified_name,
                    "start_line": func.start_line,
                    "end_line": func.end_line,
                }
            )
        duplicate_groups.append(group)

    return duplicate_groups


def detect(analyses: list[FileAnalysis], config: dict) -> list[DuplicateGroup]:
    """Detect duplicate function/method groups across the analyzed fileset."""
    params = config.get("duplication", {})
    min_tokens = params.get("min_tokens", 40)
    window_size = params.get("ngram_window", 15)
    threshold = params.get("similarity_threshold", 0.85)
    content_threshold = params.get("content_threshold", 0.5)

    functions: list[FuncEntry] = list(_iter_all_functions(analyses))
    normalized_streams, windows_per_func, eligible = _index_windows(functions, min_tokens, window_size)
    candidate_pairs = _find_candidate_pairs(windows_per_func, eligible)
    union_find, pair_similarity = _score_pairs(
        candidate_pairs,
        windows_per_func,
        normalized_streams,
        window_size,
        threshold,
        functions,
        content_threshold,
    )
    return _build_groups(union_find, pair_similarity, functions, threshold)


def to_violations(groups: list[DuplicateGroup]) -> list[Violation]:
    """Convert duplicate groups into one DUPLICATE_BLOCK Violation per group."""
    violations: list[Violation] = []
    for group in groups:
        if not group.members:
            continue
        first = group.members[0]
        severity = Severity.HIGH if group.similarity >= 0.95 else Severity.WARNING
        others = ", ".join(f"{member['file']}:{member['function']}" for member in group.members[1:])
        violations.append(
            Violation(
                code="DUPLICATE_BLOCK",
                severity=severity,
                file=first["file"],
                line=first["start_line"],
                end_line=first["end_line"],
                message=(
                    f"'{first['function']}' duplicates {len(group.members) - 1} other "
                    f"location(s) at similarity {group.similarity:.2f}: {others}"
                ),
                detail={"members": group.members, "similarity": group.similarity},
                confidence="medium",
            )
        )
    return violations


def check(analyses: list[FileAnalysis], config: dict) -> list[Violation]:
    """Run duplication detection over the whole fileset and return DUPLICATE_BLOCK violations."""
    if not config.get("duplication_detection", True):
        return []
    groups = detect(analyses, config)
    return to_violations(groups)
