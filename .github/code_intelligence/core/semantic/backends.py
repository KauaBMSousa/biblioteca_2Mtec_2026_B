"""Which questions can be answered exactly here, and which are guesses.

The index is built on tree-sitter, which parses but does not type-check.
That is the right engine for everything structural -- file inventory, LOC,
symbols, outlines, text search, duplication -- because none of those need
to know what a name refers to.

It is the wrong engine for the questions that DO:

    who calls this method?      needs the receiver's type
    what does this name mean?   needs scope + overload resolution
    is this safe to rename?     needs both

The call graph answers them by matching names, and a measured example says
what that is worth: `experiment.run()` in `autoencoderRunner.cpp` calls
`AutoencoderRunner::run`. Name matching reported ZERO callers (the symbol is
stored as `AutoencoderRunner::run`, the call site writes `run`), and after
that was patched it confidently reported the wrong one (`AsyncProgressDispatcher.run`
happens to be stored under the bare name). `clang-query`, given the same
question and the project's `compile_commands.json`, answered in one match
with a file, line and column -- because it knows `experiment` is an
`AutoencoderRunner`.

So the fix is not a better heuristic. It is to delegate the semantic
questions to the tool that already answers them per language:

    C++         clangd / libclang        (compile_commands.json)
    Python      LibCST + jedi/pyright
    JavaScript  TypeScript API / ts-morph
    Java        OpenRewrite
    PHP         Rector / nikic/php-parser

Until a backend is wired for a language, the honest thing is not to hide
the difference: every answer says which engine produced it and how much it
can be trusted. A caller must never read "0 callers, heuristic" as "dead
code" -- which is exactly the misreading this module exists to prevent.
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

#: How much an answer can be trusted, worst to best.
#:
#: `heuristic`  name matching, no type information -- may miss real
#:              references and may report unrelated ones;
#: `exact`      produced by a semantic engine that resolved types.
CONFIDENCE_HEURISTIC = "heuristic"
CONFIDENCE_EXACT = "exact"

#: One entry per language: the tool that would answer semantic questions,
#: and how to detect that it is usable here.
_BACKENDS: dict[str, dict[str, Any]] = {
    "cpp": {
        "tool": "clangd / clang-query (libclang)",
        "needs": "a compile_commands.json for the project",
        "executables": ("clangd", "clang-query"),
        "project_file": "compile_commands.json",
    },
    "python": {
        "tool": "LibCST (edits) + jedi/pyright (references)",
        "needs": "the libcst package importable",
        "module": "libcst",
    },
    "javascript": {
        "tool": "TypeScript compiler API / ts-morph",
        "needs": "node plus a tsconfig.json",
        "executables": ("node",),
        "project_file": "tsconfig.json",
    },
    "java": {
        "tool": "OpenRewrite",
        "needs": "a JDK and a build file (pom.xml / build.gradle)",
        "executables": ("java",),
        "project_file": "pom.xml",
    },
    "php": {
        "tool": "Rector / nikic-php-parser",
        "needs": "php on PATH",
        "executables": ("php",),
    },
}


def _module_available(name: str) -> bool:
    try:
        __import__(name)
    except ImportError:
        return False
    return True


def semantic_backends(root: Path) -> dict[str, Any]:
    """What semantic engine, if any, is usable for each language here.

    Reports availability only -- it never falls back to a heuristic answer
    silently, because "no backend" and "backend says no references" are
    completely different facts and only one of them means the code is
    unused.
    """
    from code_intelligence.core.semantic.registry import availability_report

    return {
        "root": str(root),
        "structural_engine": "tree-sitter (always available; no type information)",
        "exact_answers_require": "a semantic backend for that language",
        "languages": availability_report(root),
    }


def clang_references(
    root: Path, method_name: str, class_name: str | None, translation_unit: str, timeout: int = 300
) -> dict[str, Any]:
    """Exact call sites of a C++ method within one translation unit.

    Uses `clang-query` against the project's compilation database, so the
    answer accounts for the receiver's actual type. Scoped to ONE
    translation unit because a full parse costs about eleven seconds per
    TU on this project (270 TUs) -- repo-wide answers belong in a batch
    indexing pass, not in a single question.

    Raises rather than returning an empty result when the backend cannot
    run: "clangd is missing" must never be reported as "no references".
    """
    if shutil.which("clang-query") is None:
        raise RuntimeError("clang-query is not on PATH; no exact C++ backend available")
    database = root / "compile_commands.json"
    if not database.is_file():
        raise RuntimeError(f"no compile_commands.json at {root}; clang cannot know how to parse")

    inner = f'cxxMethodDecl(hasName("{method_name}")'
    if class_name:
        inner += f', ofClass(hasName("{class_name}"))'
    inner += ")"
    matcher = f"match callExpr(callee({inner}))"

    completed = subprocess.run(
        ["clang-query", "-p", str(root), translation_unit, "-c", "set output diag", "-c", matcher],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if completed.returncode != 0 and "match" not in completed.stdout:
        raise RuntimeError(f"clang-query failed: {completed.stderr.strip()[:400]}")

    sites = []
    for line in completed.stdout.splitlines():
        if ": note: \"root\" binds here" in line:
            location = line.split(": note:")[0]
            parts = location.rsplit(":", 2)
            if len(parts) == 3:
                sites.append({"file": parts[0], "line": int(parts[1]), "column": int(parts[2])})

    return {
        "backend": "clang-query",
        "confidence": CONFIDENCE_EXACT,
        "method": method_name,
        "class": class_name,
        "translation_unit": translation_unit,
        "call_sites": sites,
        "count": len(sites),
    }
