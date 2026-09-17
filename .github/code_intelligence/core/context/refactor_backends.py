"""Generic per-language transformation backends (ENHANCEME §18, §19).

`task()` / `transform()` state an operation (`rename`, `move_symbol`,
`change_type`, …). Which engine actually performs it depends on the
language: LibCST + jedi for Python, clangd + libclang for C++, the exact
semantic-edge path (tsc / javac / php-parser) or ast-grep for the rest.

Rather than the planner hard-coding "Python means `refactor.py`", each
language registers a `RefactorBackend` here. The planner resolves the
target's language, looks up the backend, and asks it to `run(op, spec)`.
`capability_matrix()` is the single source of truth for "what can this
system do to language X" — the honest answer, gaps included.

Every backend returns the transaction-engine result shape (`outcome`,
`committed`, `changed_files`, …) or a `{"outcome": "unsupported"}` dict —
it never raises for "this language can't do that".
"""

from typing import Any, Protocol, runtime_checkable

_STRUCTURAL_LANGS = ("java", "javascript", "typescript", "php", "go", "ruby", "rust")


@runtime_checkable
class RefactorBackend(Protocol):
    language: str
    tool: str

    def supports(self, op: str) -> bool: ...

    def run(self, workspace: Any, op: str, spec: dict[str, Any], commit: bool) -> dict[str, Any]: ...


def _unsupported(language: str, op: str, backend: str) -> dict[str, Any]:
    return {
        "outcome": "unsupported",
        "committed": False,
        "reason": f"the {language} backend ({backend}) has no {op!r} primitive",
        "language": language,
    }


class PythonRefactorBackend:
    language = "python"
    tool = "LibCST + jedi + ruff"

    _OPS = frozenset({
        "rename", "rename_parameter", "inline_function", "extract_function",
        "change_signature", "move_symbol", "add_import", "organize_imports",
        "remove_unused_imports", "remove_dead_code",
    })

    def supports(self, op: str) -> bool:
        return op in self._OPS

    def run(self, workspace: Any, op: str, spec: dict[str, Any], commit: bool) -> dict[str, Any]:
        c = commit
        if op == "rename":
            return _run_rename(workspace, spec, c)
        if op == "rename_parameter":
            return workspace.rename_parameter(
                spec["file"], spec["symbol"], spec["old_name"], spec["new_name"], commit=c
            )
        if op == "inline_function":
            return workspace.inline_function(spec["file"], spec["symbol"], commit=c)
        if op == "extract_function":
            return workspace.extract_function(
                spec["file"], spec["start_line"], spec["end_line"], spec["new_name"], commit=c
            )
        if op == "change_signature":
            return workspace.change_signature(
                spec["file"], spec["symbol"], parameters=spec.get("parameters"),
                add_parameter=spec.get("add_parameter"), commit=c,
            )
        if op == "move_symbol":
            return workspace.move_symbol(spec["symbol"], spec["from_file"], spec["to_file"], commit=c)
        if op == "add_import":
            return workspace.add_import(spec["file"], spec["module"], spec.get("name"), commit=c)
        if op == "organize_imports":
            return workspace.organize_imports(spec.get("scope"), commit=c)
        if op == "remove_unused_imports":
            return workspace.remove_unused_imports(spec.get("scope"), commit=c)
        if op == "remove_dead_code":
            return workspace.run_recipe("remove_dead_code", {"path_prefix": spec.get("scope")}, commit=c)
        return _unsupported(self.language, op, self.tool)


class CppRefactorBackend:
    language = "cpp"
    tool = "clangd + libclang"

    _OPS = frozenset({
        "rename", "change_return_type", "change_parameter_type", "change_type",
        "forward_declare", "add_include", "remove_include", "move_symbol",
    })

    def supports(self, op: str) -> bool:
        return op in self._OPS

    def run(self, workspace: Any, op: str, spec: dict[str, Any], commit: bool) -> dict[str, Any]:
        c = commit
        if op == "rename":
            return _run_rename(workspace, spec, c)
        if op == "change_return_type":
            return workspace.change_return_type(
                spec["file"], spec["symbol"], spec["new_type"],
                rewrite_call_sites=spec.get("rewrite_call_sites", True), commit=c,
            )
        if op == "change_parameter_type":
            return workspace.change_parameter_type(
                spec["file"], spec["symbol"], spec["parameter"], spec["new_type"], commit=c
            )
        if op == "change_type":
            return workspace.change_type(spec["file"], spec["symbol"], spec["new_type"], commit=c)
        if op == "forward_declare":
            return workspace.forward_declare(spec["file"], spec["symbol"], commit=c)
        if op == "add_include":
            return workspace.add_include(
                spec["file"], spec["include"], spec.get("system", False), commit=c
            )
        if op == "remove_include":
            return workspace.remove_include(spec["file"], spec["include"], commit=c)
        if op == "move_symbol":
            from code_intelligence.core.context import cpp_move

            return cpp_move.move_symbol(
                workspace, spec["symbol"], spec["from_file"], spec["to_file"], commit=c
            )
        return _unsupported(self.language, op, self.tool)


class StructuralRefactorBackend:
    """The fallback for languages with a semantic-edge backend but no dedicated
    refactor primitives: `rename` goes through the same exact path Python/C++
    use (it refuses without exact coverage rather than guess); structural
    edits go through ast-grep.
    """

    def __init__(self, language: str) -> None:
        self.language = language
        self.tool = "exact semantic edges + ast-grep"

    _OPS = frozenset({"rename", "replace_api"})

    def supports(self, op: str) -> bool:
        return op in self._OPS

    def run(self, workspace: Any, op: str, spec: dict[str, Any], commit: bool) -> dict[str, Any]:
        if op == "rename":
            return _run_rename(workspace, spec, commit)
        if op == "replace_api":
            return workspace.ast_transform(
                spec["pattern"], spec["replacement"], scope=spec.get("scope"), commit=commit
            )
        return _unsupported(self.language, op, self.tool)


def _run_rename(workspace: Any, spec: dict[str, Any], commit: bool) -> dict[str, Any]:
    """Shared rename: the exact cross-file path, normalized to the engine result shape."""
    from code_intelligence.core.context.editing import LowConfidenceRenameError

    escalation = ["semantic rename_symbol"]
    try:
        r = workspace.rename_symbol(spec["file"], spec["symbol"], spec["new_name"], dry_run=not commit)
    except LowConfidenceRenameError:
        escalation.append("refresh_semantic + retry")
        workspace.refresh_semantic()
        try:
            r = workspace.rename_symbol(
                spec["file"], spec["symbol"], spec["new_name"], dry_run=not commit
            )
        except LowConfidenceRenameError as exc:
            return {"outcome": "refused", "committed": False, "reason": str(exc),
                    "escalation": escalation, "needs_manual": True}
    files = {spec["file"]}
    if r.get("definition"):
        files.add(r["definition"]["file"])
    for site in r.get("call_sites", []):
        files.add(site["file"])
    r["changed_files"] = sorted(files)
    r["changed_symbols"] = 1
    r["escalation"] = escalation
    if r.get("blocked"):
        r["outcome"] = "refused"
        r["reason"] = "some sites could not be resolved unambiguously"
    elif r.get("applied"):
        r["committed"] = True
        r["outcome"] = "committed"
        workspace.index()
    else:
        r["outcome"] = "validated_dry_run"
    return r


_REGISTRY: dict[str, RefactorBackend] = {
    "python": PythonRefactorBackend(),
    "cpp": CppRefactorBackend(),
    **{lang: StructuralRefactorBackend(lang) for lang in _STRUCTURAL_LANGS},
}


def get_backend(language: str | None) -> RefactorBackend | None:
    return _REGISTRY.get((language or "").lower())


def capability_matrix() -> dict[str, list[str]]:
    """`{language: [supported ops]}` — the honest capability surface."""
    all_ops = sorted({
        "rename", "rename_parameter", "inline_function", "extract_function",
        "change_signature", "move_symbol", "add_import", "organize_imports",
        "remove_unused_imports", "remove_dead_code", "replace_api",
        "change_return_type", "change_parameter_type", "change_type",
        "forward_declare", "add_include", "remove_include",
    })
    out: dict[str, list[str]] = {}
    for lang, backend in _REGISTRY.items():
        out[lang] = [op for op in all_ops if backend.supports(op)]
    return out


__all__ = ["RefactorBackend", "capability_matrix", "get_backend"]
