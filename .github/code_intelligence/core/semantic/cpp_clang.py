"""C++ semantic backend: libclang over the project's compilation database.

Why libclang and not `clang-query`: the question is never one match, it is
every edge in the file. clang-query re-parses a translation unit per query
(~11s on a real project); libclang parses it once and the whole AST is
available, so one parse yields every call site AND the include set that
makes staleness computable.

What it fixes, concretely. In this repo `main()` contains
`return experiment.run();`. Name matching reported zero callers for
`AutoencoderRunner::run` (the call site writes `run`, the symbol is stored
`AutoencoderRunner::run`), then -- after that was patched -- confidently
reported a different C++ `run` that happened to be stored under the bare
name. libclang resolves the receiver's type and answers with the one true
callee, its definition file and line.
"""

import json
import multiprocessing
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from code_intelligence.core.semantic.protocol import BaseBackend, ExtractionResult, SemanticEdge

#: Cursor kinds that are a call to something.
_CALL_KINDS = {"CALL_EXPR", "MEMBER_REF_EXPR"}

#: Cursor kinds whose definitions are worth recording: the things an index
#: symbol can be, and therefore the things a caller can be looking for.
_DEFINITION_KINDS = {
    "FUNCTION_DECL",
    "CXX_METHOD",
    "CONSTRUCTOR",
    "DESTRUCTOR",
    "FUNCTION_TEMPLATE",
    "CLASS_DECL",
    "STRUCT_DECL",
}


def _load_index():
    """Import and configure libclang, or say precisely why it cannot be used."""
    try:
        from clang import cindex
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise RuntimeError(f"python bindings for libclang are not importable: {exc}") from exc

    if not cindex.Config.loaded:
        for candidate in ("/usr/lib/libclang.so", "/usr/lib/llvm/lib/libclang.so"):
            if Path(candidate).exists():
                cindex.Config.set_library_file(candidate)
                break
    return cindex


#: Where a compilation database turns up, in the order CMake tends to leave
#: them. The root copy is often a stale hand-copied snapshot -- in this
#: project it was six weeks older than the build directory's and missing 23
#: translation units, which would have made every call in them "not a
#: translation unit" rather than an answer.
_DATABASE_EXCLUDED = ("_deps", "node_modules", ".venv")


def find_compilation_database(root: Path) -> Path | None:
    """The NEWEST compilation database under `root`, or None.

    Searched recursively, because a workspace root is often above the C++
    project: this repo keeps sources in `software/nn/` and the database in
    `software/nn/out/build/max-performance/`. Fetched-dependency trees are
    excluded -- one of them ships its own database describing a different
    project entirely.

    Newest rather than first-found: projects accumulate several, and a
    stale one silently shrinks the answer instead of failing. Here the
    root's copy was six weeks older than the build directory's and missing
    23 translation units, which would have turned every call in them into
    "not a translation unit" -- a statement that reads like a fact about
    the code.
    """
    candidates = [
        path
        for path in root.rglob("compile_commands.json")
        if not any(part in _DATABASE_EXCLUDED for part in path.parts)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


class CppClangBackend(BaseBackend):
    """Exact C++ references, extracted per translation unit."""

    language = "cpp"
    tool = "libclang (compile_commands.json)"

    def availability(self, root: Path) -> tuple[bool, list[str]]:
        missing: list[str] = []
        try:
            _load_index()
        except RuntimeError as exc:
            missing.append(str(exc))
        if find_compilation_database(root) is None:
            missing.append(
                "no compile_commands.json found (looked in the root, build/, out/build/*/)"
            )
        return (not missing), missing

    def database_info(self, root: Path) -> dict[str, Any]:
        """Which database will be used, how big it is and how old.

        Surfaced because a stale database is the failure mode that does not
        announce itself: missing units come back as "not a translation
        unit", which reads like a fact about the code.
        """
        database = find_compilation_database(root)
        if database is None:
            return {"found": False}
        entries = json.loads(database.read_text(encoding="utf-8"))
        newest_source = max(
            (path.stat().st_mtime for path in root.rglob("*.cpp") if "out/build" not in str(path)),
            default=0.0,
        )
        return {
            "found": True,
            "path": str(database.relative_to(root)),
            "entries": len(entries),
            "mtime": database.stat().st_mtime,
            "older_than_sources": database.stat().st_mtime < newest_source,
        }

    def translation_units(self, root: Path) -> dict[str, list[str]]:
        """Map each .cpp in the compilation database to its compile arguments."""
        database_path = find_compilation_database(root)
        if database_path is None:
            raise RuntimeError("no compile_commands.json found; clang cannot know how to parse")
        database = json.loads(database_path.read_text(encoding="utf-8"))
        units: dict[str, list[str]] = {}
        for entry in database:
            file_path = Path(entry["file"])
            try:
                relpath = str(file_path.relative_to(root))
            except ValueError:
                relpath = str(file_path)
            arguments = entry.get("arguments") or entry.get("command", "").split()
            # Drop the compiler itself and the output flags: libclang wants
            # the flags, and -o/-c confuse it into writing files.
            filtered: list[str] = []
            skip_next = False
            for argument in arguments[1:]:
                if skip_next:
                    skip_next = False
                    continue
                if argument in ("-o", "-c"):
                    skip_next = argument == "-o"
                    continue
                if argument == entry["file"] or argument.endswith(file_path.name):
                    continue
                filtered.append(argument)
            units[relpath] = filtered
        return units

    def units_for(self, root: Path, files: list[str]) -> list[str]:
        """Only the .cpp files the build actually compiles.

        Headers are covered transitively: they are parsed as part of every
        translation unit that includes them, and their `semantic_unit_deps`
        entries are what make a header edit invalidate those units.
        """
        try:
            units = self.translation_units(root)
        except RuntimeError:
            return []
        return [path for path in files if path in units]

    def extract(self, root: Path, files: list[str], workers: int | None = None) -> ExtractionResult:
        """Parse the requested translation units and pull out every call edge.

        Parsed in parallel processes, because the cost is real: a TU in this
        project takes ~79 seconds (1539 in-workspace includes), so 243 of
        them are five hours serially and about twenty minutes across the
        cores of one machine. Processes rather than threads -- libclang
        holds its own global state per index, and each worker wants its own.
        """
        if workers is None:
            workers = max(1, (os.cpu_count() or 2) - 1)
        if len(files) > 1 and workers > 1:
            return self._extract_parallel(root, files, workers)
        return self._extract_serial(root, files)

    def _extract_parallel(self, root: Path, files: list[str], workers: int) -> ExtractionResult:
        """Fan the translation units out over `workers` processes."""
        merged = ExtractionResult()
        # "fork", explicitly, because both plausible defaults are wrong
        # here. Python 3.14's forkserver dies once libclang's shared library
        # is initialised (a connection reset before any unit is parsed), and
        # "spawn" re-imports the caller's __main__ module -- so any caller
        # without an `if __name__ == "__main__"` guard watches its workers
        # die instead of its code run. Fork inherits the interpreter as it
        # stands and asks nothing of the caller.
        context = multiprocessing.get_context("fork")
        with ProcessPoolExecutor(max_workers=workers, mp_context=context) as pool:
            futures = {pool.submit(_extract_one, str(root), unit): unit for unit in files}
            for future in as_completed(futures):
                unit = futures[future]
                try:
                    edges, dependencies, failure, definitions = future.result()
                except Exception as exc:  # a crashed worker is a unit failure
                    merged.failures[unit] = f"libclang worker died: {exc}"
                    continue
                if failure:
                    merged.failures[unit] = failure
                    continue
                merged.dependencies[unit] = dependencies
                merged.edges.extend(SemanticEdge(**edge) for edge in edges)
                merged.definitions.extend(definitions)
        return merged

    def _extract_serial(self, root: Path, files: list[str]) -> ExtractionResult:
        """One process, one TU at a time (used for a single unit, and by workers)."""
        cindex = _load_index()
        index = cindex.Index.create()
        units = self.translation_units(root)
        result = ExtractionResult()

        for relpath in files:
            arguments = units.get(relpath)
            if arguments is None:
                result.failures[relpath] = "not in compile_commands.json (not a translation unit)"
                continue
            try:
                translation_unit = index.parse(str(root / relpath), args=arguments)
            except Exception as exc:  # pragma: no cover - clang crash path
                result.failures[relpath] = f"libclang failed: {exc}"
                continue

            includes = []
            for include in translation_unit.get_includes():
                include_path = Path(include.include.name)
                try:
                    includes.append(str(include_path.relative_to(root)))
                except ValueError:
                    continue  # a system header: outside the workspace, never edited here
            result.dependencies[relpath] = includes

            edges, definitions = self._walk(translation_unit.cursor, root, relpath, cindex)
            result.edges.extend(edges)
            result.definitions.extend(definitions)

        return result

    def _walk(
        self, cursor, root: Path, relpath: str, cindex
    ) -> tuple[list[SemanticEdge], list[tuple[str, str, int, str]]]:
        """Every resolved call inside `relpath`'s own text, and what it defines.

        The definitions matter as much as the calls: they are how an index
        symbol (a .cpp definition) is joined to the USR that callers
        reference through a header.
        """
        edges: list[SemanticEdge] = []
        definitions: list[tuple[str, str, int, str]] = []
        # `experiment.run()` produces BOTH a CALL_EXPR and a MEMBER_REF_EXPR
        # for the same call, at DIFFERENT columns (the call starts at the
        # receiver, the member reference at the dot), so the key has to be
        # what was resolved rather than where the node began -- otherwise
        # every method call is reported twice.
        seen: set[tuple[int, str, str | None, int | None]] = set()
        stack = [cursor]
        while stack:
            node = stack.pop()
            stack.extend(node.get_children())

            location = node.location
            if location.file is None:
                continue
            # Only calls written in THIS file: a translation unit drags in
            # thousands of header lines, and attributing those to the .cpp
            # would make every TU look like it calls the whole standard
            # library.
            try:
                if str(Path(location.file.name).relative_to(root)) != relpath:
                    continue
            except ValueError:
                continue

            # Record definitions written in this file, keyed by USR.
            if node.is_definition() and node.kind.name in _DEFINITION_KINDS:
                usr = node.get_usr()
                if usr:
                    definitions.append((usr, relpath, location.line, node.spelling))

            if node.kind.name not in _CALL_KINDS:
                continue
            referenced = node.referenced
            if referenced is None:
                continue

            definition = referenced.get_definition() or referenced
            to_file: str | None = None
            if definition.location.file is not None:
                try:
                    to_file = str(Path(definition.location.file.name).relative_to(root))
                except ValueError:
                    to_file = None

            key = (
                location.line,
                referenced.spelling,
                to_file,
                definition.location.line if to_file else None,
            )
            if key in seen:
                continue
            seen.add(key)
            edges.append(
                SemanticEdge(
                    from_file=relpath,
                    from_line=location.line,
                    from_column=location.column,
                    to_name=referenced.spelling,
                    to_file=to_file,
                    to_line=definition.location.line if to_file else None,
                    kind="call",
                    to_usr=referenced.canonical.get_usr() or None,
                )
            )
        return edges, definitions


def build() -> CppClangBackend:
    return CppClangBackend()


def _extract_one(
    root: str, unit: str
) -> tuple[list[dict[str, Any]], list[str], str | None, list[tuple[str, str, int, str]]]:
    """Worker entry point: one translation unit, in its own process.

    Returns plain data (not dataclasses) because it crosses a process
    boundary, and the failure as a value rather than an exception so a
    single unparseable TU cannot take the batch down.
    """
    backend = CppClangBackend()
    result = backend._extract_serial(Path(root), [unit])
    failure = result.failures.get(unit)
    edges = [
        {
            "from_file": edge.from_file,
            "from_line": edge.from_line,
            "from_column": edge.from_column,
            "to_name": edge.to_name,
            "to_file": edge.to_file,
            "to_line": edge.to_line,
            "kind": edge.kind,
            "to_usr": edge.to_usr,
        }
        for edge in result.edges
    ]
    return edges, result.dependencies.get(unit, []), failure, result.definitions
