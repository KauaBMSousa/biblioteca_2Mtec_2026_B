"""C++ `#include` queries and edits (ENHANCEME §V), syntactic.

Include management does not need a type checker — it needs to read and
rewrite the `#include` block correctly (bracket vs quote, ordering,
duplicates, the `#pragma once` / include-guard prologue). These operate
over the indexed C++ files' text; the edits go through the transaction
engine like every other mutation.
"""

import re
from typing import Any

from code_intelligence.core.exec.errors import UnsupportedLanguageError

_CPP_LANG = "cpp"
_INCLUDE_RE = re.compile(r'^(\s*#\s*include\s*)([<"])([^>"]+)([>"])(.*)$')
_GUARD_PROLOGUE = re.compile(r"^\s*(#pragma\s+once|#ifndef\s+\w+|#define\s+\w+)\s*$")


def _cpp_files(store: Any, path_prefix: str | None = None) -> list[str]:
    return [
        row["path"]
        for row in store.list_files()
        if row["language"] == _CPP_LANG and (path_prefix is None or row["path"].startswith(path_prefix))
    ]


def _require_cpp(workspace: Any, file: str) -> None:
    row = workspace.store.get_file(file)
    if row is None or row["language"] != _CPP_LANG:
        raise UnsupportedLanguageError(f"C++ files only (got {file!r})")


def parse_includes(text: str) -> list[dict[str, Any]]:
    """Every `#include` directive in `text`, with 1-based line and bracket kind."""
    out: list[dict[str, Any]] = []
    for i, line in enumerate(text.splitlines(), start=1):
        match = _INCLUDE_RE.match(line)
        if match:
            out.append(
                {
                    "line": i,
                    "target": match.group(3),
                    "system": match.group(2) == "<",
                }
            )
    return out


def find_includes(workspace: Any, file: str) -> dict[str, Any]:
    """The `#include` directives in one C++ file, and which resolve inside the workspace."""
    _require_cpp(workspace, file)
    text = (workspace.root / file).read_text(encoding="utf-8", errors="replace")
    known = set(_cpp_files(workspace.store))
    includes = parse_includes(text)
    for inc in includes:
        inc["resolved"] = next(
            (path for path in known if path.endswith("/" + inc["target"]) or path == inc["target"]),
            None,
        )
    return {
        "file": file,
        "count": len(includes),
        "system": [i["target"] for i in includes if i["system"]],
        "local": [i for i in includes if not i["system"]],
    }


def include_graph(workspace: Any, path_prefix: str | None = None, limit: int = 40) -> dict[str, Any]:
    """The workspace `#include` DAG: what each C++ file includes, and what is included most."""
    files = _cpp_files(workspace.store, path_prefix)
    known = set(files) | set(_cpp_files(workspace.store))
    edges: dict[str, list[str]] = {}
    included_by: dict[str, int] = {}
    for relpath in files:
        text = (workspace.root / relpath).read_text(encoding="utf-8", errors="replace")
        targets: list[str] = []
        for inc in parse_includes(text):
            if inc["system"]:
                continue
            resolved = next(
                (p for p in known if p.endswith("/" + inc["target"]) or p == inc["target"]), None
            )
            if resolved:
                targets.append(resolved)
                included_by[resolved] = included_by.get(resolved, 0) + 1
        if targets:
            edges[relpath] = sorted(set(targets))
    hottest = sorted(included_by.items(), key=lambda kv: -kv[1])[:limit]
    return {
        "files": len(files),
        "files_with_local_includes": len(edges),
        "edges": dict(sorted(edges.items())[:limit]),
        "most_included": [{"file": f, "included_by": n} for f, n in hottest],
    }


def _insertion_point(lines: list[str]) -> int:
    """Line index (0-based) to insert a new `#include` before."""
    last_include = -1
    prologue_end = 0
    for i, line in enumerate(lines):
        if _INCLUDE_RE.match(line):
            last_include = i
        elif _GUARD_PROLOGUE.match(line):
            prologue_end = i + 1
    if last_include >= 0:
        return last_include + 1
    return prologue_end


def add_include(
    workspace: Any,
    file: str,
    include: str,
    system: bool = False,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Add `#include <include>` / `#include "include"` to a C++ file, ordered and de-duplicated."""
    _require_cpp(workspace, file)
    target = include.strip().strip('<>"')
    text = (workspace.root / file).read_text(encoding="utf-8", errors="replace")
    if any(inc["target"] == target and inc["system"] == system for inc in parse_includes(text)):
        return {"outcome": "no_change", "committed": False, "rolled_back": False, "include": target}

    lines = text.splitlines()
    directive = f"#include <{target}>" if system else f'#include "{target}"'

    # Insert into the matching bracket group, keeping it alphabetical; fall
    # back to the general insertion point when that group is empty.
    group = [
        (i, m.group(3))
        for i, line in enumerate(lines)
        if (m := _INCLUDE_RE.match(line)) and (m.group(2) == "<") == system
    ]
    if group:
        at = next((i for i, tgt in group if tgt > target), group[-1][0] + 1)
    else:
        at = _insertion_point(lines)
    lines.insert(at, directive)
    new_text = "\n".join(lines) + ("\n" if text.endswith("\n") else "")

    result = workspace.execute_transaction(
        [{"op": "write_file", "file": file, "new_text": new_text}],
        validate=validate or ["syntax"],
        commit=commit,
    )
    result["include"] = target
    return result


def remove_include(
    workspace: Any,
    file: str,
    include: str,
    *,
    validate: list[str] | None = None,
    commit: bool = True,
) -> dict[str, Any]:
    """Remove a `#include` line from a C++ file."""
    _require_cpp(workspace, file)
    target = include.strip().strip('<>"')
    text = (workspace.root / file).read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    kept = [
        line
        for line in lines
        if not ((m := _INCLUDE_RE.match(line)) and m.group(3) == target)
    ]
    if len(kept) == len(lines):
        return {"outcome": "no_change", "committed": False, "rolled_back": False, "include": target}
    new_text = "\n".join(kept) + ("\n" if text.endswith("\n") else "")
    result = workspace.execute_transaction(
        [{"op": "write_file", "file": file, "new_text": new_text}],
        validate=validate or ["syntax"],
        commit=commit,
    )
    result["removed_include"] = target
    return result


__all__ = ["find_includes", "include_graph", "add_include", "remove_include", "parse_includes"]
