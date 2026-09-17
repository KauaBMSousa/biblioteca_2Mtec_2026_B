"""Procedural-monolith detection (fixme.md §4.6): PROCEDURAL_MONOLITH.

For files with no classes, detection is based on:
  - absence of classes (a precondition, not by itself a violation);
  - the number of top-level functions grouped in the file;
  - a cohesion signal: how much identifier vocabulary (parameter/variable
    names) is shared between those functions.

A file with many top-level functions that share almost no identifiers is a
grab-bag of unrelated responsibilities crammed into one file — flagged as
PROCEDURAL_MONOLITH. A file with many top-level functions that *do* share
vocabulary (helpers cooperating on the same data) is treated as a
legitimately cohesive procedural module and left alone.

Ported unchanged (besides import paths, plus a `confidence="medium"` tag —
this is a heuristic cohesion signal, not a deterministic threshold) from
`tools/code_quality/code_quality/rules/procedural.py`.
"""

from itertools import combinations

from code_intelligence.core.diagnostics.violation import Severity, Violation
from code_intelligence.core.parser.ir import FileAnalysis, FunctionInfo

#: Below this many top-level functions, there isn't enough signal to judge
#: cohesion meaningfully — a 2-3 function utility file is normal.
_MIN_FUNCTIONS_FOR_CHECK = 4

#: Average pairwise Jaccard similarity of identifier vocabularies below this
#: is treated as "these functions don't appear to cooperate".
_COHESION_THRESHOLD = 0.12


#: Cohesion is about what data a function *declares and operates on*
#: (parameters/assigned variables), matching this rule's original,
#: already-tuned behavior. `"function"`/`"class"` identifiers were added
#: later (for `core/dependencies`' name-matching index) and are
#: deliberately excluded here too, the same way `rules/naming.py` excludes
#: them — otherwise every call a function happens to make would count
#: toward "shared vocabulary" and silently shift which files get flagged.
#: `"reference"` is included on purpose: this rule asks what vocabulary a
#: function TOUCHES, not what it declares, so a name it merely reads still
#: counts. (The naming rule asks the opposite question and takes only the
#: declarations.)
_VOCABULARY_KINDS = {"parameter", "variable", "loop_index", "reference"}


def _identifier_vocabulary(func: FunctionInfo) -> set[str]:
    """The set of parameter/variable names a function references."""
    return {ident.name for ident in func.identifiers if ident.kind in _VOCABULARY_KINDS}


def _average_pairwise_jaccard(functions: list[FunctionInfo]) -> float:
    """Average Jaccard similarity of identifier vocabularies across all function pairs."""
    vocabularies = [_identifier_vocabulary(func) for func in functions]
    pairs = list(combinations(vocabularies, 2))
    if not pairs:
        return 1.0  # nothing to compare; don't flag
    scores = []
    for left, right in pairs:
        union = left | right
        if not union:
            scores.append(0.0)
            continue
        scores.append(len(left & right) / len(union))
    return sum(scores) / len(scores)


def check(analysis: FileAnalysis, config: dict) -> list[Violation]:
    """Flag a file of many low-cohesion top-level functions as PROCEDURAL_MONOLITH."""
    if not config["rules"].get("procedural_separation"):
        return []
    if analysis.classes:
        return []  # only applies to purely procedural files
    functions = analysis.top_level_functions
    if len(functions) < _MIN_FUNCTIONS_FOR_CHECK:
        return []

    cohesion = _average_pairwise_jaccard(functions)
    if cohesion >= _COHESION_THRESHOLD:
        return []

    return [
        Violation(
            code="PROCEDURAL_MONOLITH",
            severity=Severity.HIGH,
            file=analysis.relpath,
            line=functions[0].start_line,
            end_line=functions[-1].end_line,
            message=(
                f"File '{analysis.relpath}' groups {len(functions)} top-level functions "
                f"with low shared vocabulary (cohesion={cohesion:.2f}) — looks like "
                "multiple unrelated responsibilities in one file"
            ),
            detail={"function_count": len(functions), "cohesion": round(cohesion, 3)},
            column=functions[0].start_column,
            end_column=functions[-1].end_column,
            confidence="medium",
        )
    ]
