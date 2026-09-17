"""Defines the LanguageAdapter contract every per-language adapter implements."""

from abc import ABC, abstractmethod
from pathlib import Path

from code_intelligence.core.parser.ir import FileAnalysis


class LanguageAdapter(ABC):
    """Turns one source file's text into a :class:`FileAnalysis`.

    Implementations must never raise on malformed/unparseable input — on a
    parse failure they fall back to a regex/brace-count LOC-only analysis
    with ``parse_fallback_used=True`` rather than crashing the run.
    """

    #: Human-readable language name, e.g. "python", "cpp".
    language: str

    #: File extensions (including the leading dot) this adapter handles.
    extensions: tuple[str, ...]

    def can_handle(self, path: Path) -> bool:
        """Return True when this adapter should analyze ``path``.

        Args:
            path: The candidate file path.
        """
        return path.suffix in self.extensions

    def blocks_containing(self, source: str, line: int) -> list[dict] | None:
        """Every syntactic block containing `line`, outermost first.

        Each entry is ``{"type", "start_line", "end_line"}`` with 1-based,
        inclusive lines. Returns None when this adapter cannot answer (no
        usable parse); callers must not guess a range in that case -- an
        approximate block boundary is how an extract-a-function refactor
        silently drops or duplicates code.

        Optional: the base implementation declines. Adapters with a real
        parse tree override it.
        """
        return None

    def outline(self, source: str, start_line: int, end_line: int, max_depth: int = 2) -> list[dict] | None:
        """The statement structure of a line span, as a shallow tree.

        Each entry is ``{"type", "start_line", "end_line", "depth",
        "text"}`` where `text` is the construct's own first line. Control
        flow only -- loops, branches, try blocks, nested scopes -- not every
        expression, because the purpose is to see the SHAPE of a long
        function without reading it.

        Returns None when this adapter cannot answer. Optional, like
        `blocks_containing`.
        """
        return None

    @abstractmethod
    def analyze(self, path: Path, source: str) -> FileAnalysis:
        """Analyze one file's source text and return its IR.

        Args:
            path: Absolute path to the file (used for path metadata only —
                implementations must analyze ``source``, not re-read the
                file, so callers control I/O and hashing).
            source: The file's full text content.

        Returns:
            A populated :class:`FileAnalysis`. ``relpath``/``file_hash`` are
            left for the caller (the discovery/analysis pipeline) to fill in
            since the adapter doesn't know the analysis root.
        """
        raise NotImplementedError
