"""Defines :class:`FileAnalysis`, the top-level per-file IR record."""

from dataclasses import dataclass, field

from code_intelligence.core.parser.ir.class_info import ClassInfo
from code_intelligence.core.parser.ir.function_info import FunctionInfo
from code_intelligence.core.parser.ir.import_info import ImportInfo


@dataclass(slots=True)
class FileAnalysis:
    """Everything a language adapter extracts from one source file.

    Every rule module operates only on ``FileAnalysis`` (or, once indexed,
    the equivalent index-backed row set) — never on tree-sitter nodes or
    ``ast`` nodes directly. This boundary keeps rules language-agnostic and
    keeps parser dependencies confined to ``core/parser/``.

    Attributes:
        path: Absolute filesystem path.
        relpath: Path relative to the analysis root, using ``/`` separators.
        language: One of ``"python"``, ``"cpp"``, ``"java"``, ``"php"``,
            ``"javascript"``.
        physical_lines: Total physical line count.
        logical_code_lines: Lines containing actual code (excludes blank
            lines and full-line comments).
        comment_lines: Lines that are wholly comments.
        blank_lines: Lines with no non-whitespace content.
        classes: Type definitions (class/struct/interface/enum/...) found in
            the file, in declaration order.
        top_level_functions: Free functions not owned by any class, in
            declaration order.
        imports: Import/include/require references found in the file.
        parse_ok: Whether the primary parser (tree-sitter/``ast``)
            successfully produced a tree.
        parse_fallback_used: Whether the regex/brace-count fallback path
            was used instead of (or after) the primary parser.
        parse_error: The primary parser's error message, if any.
        is_generated: Whether generated-code heuristics matched this file.
        is_test_file: Whether the configured test-file patterns matched.
        is_config_file: Whether the configured config-file patterns matched.
        is_embedded_listing: Whether the file is a document listing rather
            than source -- a ``.py``/``.cpp`` whose content is a LaTeX
            ``\begin{lstlisting}`` (or minted/verbatim) block, meant to be
            \input into a paper. It cannot parse and never could; saying
            so is different from reporting a parse failure.
        file_hash: Content hash (blake2b hex digest) used for cache keys and
            for the deterministic duplication-detection hashing.
        parse_confidence: ``"high"`` when the primary AST/tree-sitter parser
            succeeded, ``"low"`` when the regex/brace-count fallback was
            used instead (mirrors ``parse_fallback_used`` but as an
            agent-facing confidence label).
        ast_hash: sha256 of a canonical serialization of the file's
            extracted symbol list — changes only when the *structure*
            changes, not on every byte edit (e.g. a comment-only edit
            changes ``file_hash`` but not ``ast_hash``).
    """

    path: str
    relpath: str
    language: str
    physical_lines: int
    logical_code_lines: int
    comment_lines: int
    blank_lines: int
    classes: list[ClassInfo] = field(default_factory=list)
    top_level_functions: list[FunctionInfo] = field(default_factory=list)
    imports: list[ImportInfo] = field(default_factory=list)
    parse_ok: bool = True
    parse_fallback_used: bool = False
    parse_error: str | None = None
    is_generated: bool = False
    is_test_file: bool = False
    is_config_file: bool = False
    is_embedded_listing: bool = False
    file_hash: str = ""
    parse_confidence: str = "high"
    ast_hash: str = ""
