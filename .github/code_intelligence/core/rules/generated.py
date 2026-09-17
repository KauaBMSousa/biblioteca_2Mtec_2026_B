"""Generated/test/config-file categorization heuristics (fixme.md §8).

Cheap, marker-based heuristics only — no deep analysis — per fixme.md §8's
"heuristicas leves (sem custo alto)" requirement. This module doesn't
itself produce Violation objects; it annotates `FileAnalysis.is_generated`
/ `is_test_file` / `is_config_file`, which other rules and the report
generator use to contextualize (not silently suppress) findings.

Ported unchanged (besides import paths) from
`tools/code_quality/code_quality/rules/generated.py`.
"""

import fnmatch
from pathlib import Path

from code_intelligence.core.parser.ir import FileAnalysis

#: Only the first N lines are scanned for generated-code markers — matches
#: real-world convention (headers) and keeps the heuristic O(1) per file.
_HEADER_SCAN_LINES = 20


def _is_generated(source: str, markers: list[str]) -> bool:
    """Return True when any configured marker string appears in the file's header."""
    header_lines = source.splitlines()[:_HEADER_SCAN_LINES]
    header = "\n".join(header_lines)
    return any(marker in header for marker in markers)


def _matches_any_pattern(basename: str, patterns: list[str]) -> bool:
    """Return True when `basename` matches any of the fnmatch-style patterns."""
    return any(fnmatch.fnmatch(basename, pattern) for pattern in patterns)


def _is_test_file(relpath: str, patterns: list[str], test_dir_names: list[str]) -> bool:
    """Whether this file holds tests, by NAME or by LOCATION.

    Name alone is not enough, and the miss is silent rather than loud. This
    project's 3097 tests all live in files called ``*_gtest.cpp``, which the
    conventional pattern ``*_test.cpp`` does not match -- ``_gtest.cpp`` ends
    in ``test.cpp`` but the character before ``test`` is ``g``, not ``_``::

        fnmatch("opencl_tensor_backend_gtest.cpp", "*_test.cpp")  -> False
        fnmatch("opencl_tensor_backend_gtest.cpp", "*_gtest.cpp") -> True

    Nothing fails when that happens. The files are still indexed, still
    parsed, still reported -- they are merely filed as production code. Any
    rule that asks "is this covered by a test?" then answers "no" for the
    entire codebase, which reads exactly like a real finding.

    So location counts too: a file under a directory named ``tests`` holds
    tests whatever it is called. The two signals are ORed because either one
    alone is sufficient evidence, and a project that satisfies neither is
    telling us it has no tests here.
    """
    path = Path(relpath)
    if _matches_any_pattern(path.name, patterns):
        return True
    return any(segment in test_dir_names for segment in path.parent.parts)


def _is_embedded_listing(source: str, markers: list[str]) -> bool:
    """Return True when the file *opens* a document-listing environment.

    A ``.py`` whose first real line is ``\\begin{lstlisting}[language=Python]``
    is a LaTeX fragment that a paper ``\\input``s, not a module: it has never
    parsed as Python and never will. Reporting that as a parse failure
    blames the code for its extension, and buries the real parse failures
    among files that were never source to begin with.

    Anchored to the first non-blank line on purpose: a listing marker
    *inside* a real source file is a string or a comment, and says nothing
    about the file.
    """
    for line in source.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        return any(stripped.startswith(marker) for marker in markers)
    return False


def annotate(analysis: FileAnalysis, source: str, config: dict) -> FileAnalysis:
    """Set is_generated/is_test_file/is_config_file/is_embedded_listing in place.

    Returns the same `analysis` instance for convenient chaining.
    """
    basename = Path(analysis.relpath).name

    if config.get("generated_code_detection", True):
        analysis.is_generated = _is_generated(source, config.get("generated_code_markers", []))

    analysis.is_test_file = _is_test_file(
        analysis.relpath,
        config.get("test_file_patterns", []),
        config.get("test_dir_names", []),
    )
    analysis.is_config_file = _matches_any_pattern(basename, config.get("config_file_patterns", []))
    analysis.is_embedded_listing = _is_embedded_listing(
        source, config.get("embedded_listing_markers", [])
    )
    return analysis
