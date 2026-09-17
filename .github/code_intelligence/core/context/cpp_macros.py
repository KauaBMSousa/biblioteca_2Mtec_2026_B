"""C++ macro discovery (ENHANCEME §V), syntactic.

`find_macro(name)` locates the `#define` and every textual use across the
indexed C++ files. It is a text scan, not a preprocessor: it cannot tell a
use inside a disabled `#if 0` block from a live one, and it reports that
limitation rather than hiding it. `macro_expansion_context` (in
`core/semantic/cpp_queries.py`) is the libclang-backed answer for one
exact location.
"""

import re
from typing import Any

from code_intelligence.core.exec.errors import UnsupportedLanguageError

_CPP_LANG = "cpp"


def find_macro(workspace: Any, name: str, path_prefix: str | None = None, limit: int = 100) -> dict[str, Any]:
    """The `#define` for `name` and every textual use across the workspace's C++ files."""
    if not name.isidentifier():
        raise UnsupportedLanguageError(f"{name!r} is not a macro identifier")
    define_re = re.compile(rf"^\s*#\s*define\s+{re.escape(name)}\b(.*)$")
    use_re = re.compile(rf"\b{re.escape(name)}\b")

    definitions: list[dict[str, Any]] = []
    uses: list[dict[str, Any]] = []
    for row in workspace.store.list_files():
        if row["language"] != _CPP_LANG:
            continue
        if path_prefix and not row["path"].startswith(path_prefix):
            continue
        text = (workspace.root / row["path"]).read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), start=1):
            define_match = define_re.match(line)
            if define_match:
                body = define_match.group(1).strip()
                definitions.append(
                    {
                        "file": row["path"],
                        "line": i,
                        "function_like": body.startswith("("),
                        "body": body[:200] or None,
                    }
                )
            elif use_re.search(line) and not line.lstrip().startswith("#define"):
                if len(uses) < limit:
                    uses.append({"file": row["path"], "line": i, "text": line.strip()[:160]})

    return {
        "macro": name,
        "definitions": definitions,
        "definition_count": len(definitions),
        "use_count": len(uses),
        "uses": uses,
        "caveat": "textual scan — cannot distinguish uses inside inactive #if blocks",
    }


__all__ = ["find_macro"]
