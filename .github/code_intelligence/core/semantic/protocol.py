"""The contract every semantic backend implements, and what it produces.

A semantic backend answers the questions tree-sitter cannot, because they
need types: which function does `obj.method()` actually call, what does this
name refer to here. Each language has a real tool for that, and this module
is the shape they all present to the index:

    C++         libclang / clang-query        compile_commands.json
    Python      jedi (references) + LibCST    the source tree
    JavaScript  TypeScript compiler API       tsconfig.json
    Java        OpenRewrite / javac           pom.xml or build.gradle
    PHP         nikic/php-parser (Rector)     composer

The unit of work is deliberately a **batch over files**, not a query per
question. Parsing one C++ translation unit costs ~11s on a real project;
answering "who calls this?" by parsing on demand is unusable, while
extracting every edge once and storing it keeps queries instant and makes
them exact.

`stale_after` is what allows the index to stay honest as code changes: a
backend reports which OTHER files a unit depended on (a C++ TU's include
set, a Python module's imports), so editing a header invalidates every
translation unit that read it -- not just the header itself. Without that,
exact-looking edges silently rot, which is worse than heuristic edges that
announce themselves.
"""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


@dataclass(frozen=True, slots=True)
class SemanticEdge:
    """One exactly-resolved reference from a call site to a definition.

    Positions are the CALL SITE's, because that is what a reader needs to
    navigate to; `to_file`/`to_line` locate the definition it resolved to.
    """

    from_file: str
    from_line: int
    from_column: int
    to_name: str
    to_file: str | None
    to_line: int | None
    kind: str = "call"
    #: Stable cross-unit identity of the callee, when the language's engine
    #: has one (clang's USR). It is what lets a call that sees a header
    #: declaration be matched to the definition the index holds.
    to_usr: str | None = None


@dataclass(slots=True)
class ExtractionResult:
    """What one backend produced for one batch of files."""

    edges: list[SemanticEdge] = field(default_factory=list)
    #: unit -> the other files it read (includes/imports). Editing any of
    #: them makes the unit's edges stale.
    dependencies: dict[str, list[str]] = field(default_factory=dict)
    #: Files the backend could not analyze, with the reason. Never silent:
    #: a unit that failed to parse must not look like a unit with no calls.
    failures: dict[str, str] = field(default_factory=dict)
    #: `(usr, file, line, name)` for every definition the backend saw, so an
    #: index symbol can be joined to the USR its callers reference.
    definitions: list[tuple[str, str, int, str]] = field(default_factory=list)


@runtime_checkable
class SemanticBackend(Protocol):
    """A per-language engine that resolves references with type information."""

    #: The `FileAnalysis.language` value this backend serves.
    language: str
    #: Human-readable name of the underlying tool, for reporting.
    tool: str

    def availability(self, root: Path) -> tuple[bool, list[str]]:
        """Whether this backend can run here, and what is missing if not.

        Never raises: a missing toolchain is a fact to report, not an error
        to crash on.
        """
        ...

    def units_for(self, root: Path, files: list[str]) -> list[str]:
        """Which of `files` this backend can analyze as units of its own.

        Not every file of a language is a unit of compilation: a C++ header
        is analyzed only as part of the translation units that include it,
        and reporting it as a failed unit would make the coverage report
        say the backend is broken when it is working exactly as intended.
        """
        return list(files)

    def extract(self, root: Path, files: list[str]) -> ExtractionResult:
        """Resolve references for `files` (workspace-relative paths).

        Implementations may parse more than `files` when the language's unit
        of compilation is bigger (a C++ TU, a TS project), but must report
        edges only for the requested files' call sites.
        """
        ...


class BaseBackend:
    """Defaults every backend shares.

    A `Protocol` describes the shape but supplies nothing, so the default
    `units_for` has to live somewhere real -- and it is the right default
    for four of the five languages (only C++ has files that are not units
    of their own).
    """

    language: str = ""
    tool: str = ""

    def units_for(self, root: Path, files: list[str]) -> list[str]:
        """Every file is its own unit, unless a backend says otherwise."""
        return list(files)
