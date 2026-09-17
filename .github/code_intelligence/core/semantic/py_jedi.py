"""Python semantic backend: jedi for resolution, LibCST available for edits.

Python's ambiguity is the same shape as C++'s, without the compilation
database: `handler.run()` cannot be resolved by name, because what `handler`
is depends on how it was built. jedi does the inference (the same engine
editors use for go-to-definition), so a call resolves to the definition it
actually reaches.

Scope is the file: jedi infers within a project, so a whole-file pass yields
every call site with its resolved definition in one traversal, and the
module's imports are the dependency set that makes those edges stale.
"""

import ast
from pathlib import Path
from typing import Any

from code_intelligence.core.semantic.protocol import BaseBackend, ExtractionResult, SemanticEdge


def _load_jedi():
    try:
        import jedi
    except ImportError as exc:  # pragma: no cover - depends on the machine
        raise RuntimeError(f"the jedi package is not importable: {exc}") from exc
    return jedi


class PythonJediBackend(BaseBackend):
    """Exact Python references via jedi's inference engine."""

    language = "python"
    tool = "jedi (inference) + LibCST (edits)"

    def availability(self, root: Path) -> tuple[bool, list[str]]:
        missing: list[str] = []
        try:
            _load_jedi()
        except RuntimeError as exc:
            missing.append(str(exc))
        return (not missing), missing

    def extract(self, root: Path, files: list[str]) -> ExtractionResult:
        jedi = _load_jedi()
        project = jedi.Project(str(root))
        result = ExtractionResult()

        for relpath in files:
            path = root / relpath
            try:
                source = path.read_text(encoding="utf-8")
            except OSError as exc:
                result.failures[relpath] = f"unreadable: {exc}"
                continue

            try:
                tree = ast.parse(source)
            except SyntaxError as exc:
                # A file that does not parse has no resolvable calls, and
                # saying so is the point: "0 edges" and "could not be
                # analyzed" must not look alike.
                result.failures[relpath] = f"syntax error at line {exc.lineno}: {exc.msg}"
                continue

            result.dependencies[relpath] = sorted(self._imported_files(tree, root, relpath))

            script = jedi.Script(code=source, path=str(path), project=project)
            seen: set[tuple[int, int, str]] = set()
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                target = node.func
                # The name column jedi needs is the attribute/name itself,
                # not the start of the expression: for `a.b.run()` the
                # answer depends on asking about `run`, not about `a`.
                line, column, label = self._call_position(target)
                if line is None:
                    continue
                key = (line, column, label)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    definitions = script.goto(line=line, column=column, follow_imports=True)
                except Exception:  # pragma: no cover - jedi internal failure
                    continue
                for definition in definitions[:1]:
                    to_file: str | None = None
                    if definition.module_path:
                        try:
                            to_file = str(Path(definition.module_path).relative_to(root))
                        except ValueError:
                            to_file = None
                    result.edges.append(
                        SemanticEdge(
                            from_file=relpath,
                            from_line=line,
                            from_column=column,
                            to_name=definition.name,
                            to_file=to_file,
                            to_line=definition.line,
                        )
                    )
        return result

    @staticmethod
    def _call_position(target: ast.expr) -> tuple[int | None, int | None, str]:
        """Line/column of the NAME being called, and that name."""
        if isinstance(target, ast.Name):
            return target.lineno, target.col_offset, target.id
        if isinstance(target, ast.Attribute):
            # `obj.method()` -- ask about `method`, which sits after the dot.
            return target.end_lineno, max(0, (target.end_col_offset or 1) - len(target.attr)), target.attr
        return None, None, ""

    @staticmethod
    def _imported_files(tree: ast.AST, root: Path, relpath: str) -> set[str]:
        """Workspace files this module imports, for staleness.

        Best effort by module path: an import of a module outside the
        workspace is not a staleness source, because it cannot be edited
        here.
        """
        found: set[str] = set()
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
                names = [node.module]
            for name in names:
                candidate = root / (name.replace(".", "/") + ".py")
                if candidate.is_file():
                    found.add(str(candidate.relative_to(root)))
                package = root / name.replace(".", "/") / "__init__.py"
                if package.is_file():
                    found.add(str(package.relative_to(root)))
        return found


def build() -> PythonJediBackend:
    return PythonJediBackend()
