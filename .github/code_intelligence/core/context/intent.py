"""Parse a natural-language refactoring intent into a normalized operation (ENHANCEME §5, §7).

The agent should say *what*, not *how*. This maps a handful of common,
unambiguous phrasings onto the operation the planner will resolve and
execute. It is deliberately a small rule set, not an LLM: an intent it
does not recognize comes back as `{"op": "unknown"}` with the phrasings it
does understand, and the agent falls back to an explicit `transform` /
`transaction` call.

Every recognized result carries a `confidence`:
`high` — the phrasing is exact and the operation is well-defined;
`medium` — recognized but a parameter had to be guessed (e.g. scope).
"""

import re
from typing import Any

_RULES: list[tuple[re.Pattern, str, Any]] = []


def _rule(pattern: str, op: str, build) -> None:
    _RULES.append((re.compile(pattern, re.IGNORECASE), op, build))


_rule(
    r"^\s*rename\s+(?P<a>[\w:.]+)\s+(?:to|->|→)\s+(?P<b>\w+)\s*$",
    "rename",
    lambda m: {"symbol": m["a"], "new_name": m["b"]},
)
_rule(
    r"^\s*rename\s+(?:the\s+)?parameter\s+(?P<a>\w+)\s+(?:of|in)\s+(?P<sym>[\w:.]+)\s+(?:to|->)\s+(?P<b>\w+)\s*$",
    "rename_parameter",
    lambda m: {"symbol": m["sym"], "old_name": m["a"], "new_name": m["b"]},
)
_rule(
    r"^\s*(?:replace|migrate)\s+(?:calls?\s+to\s+|the\s+)?(?:deprecated\s+)?(?:api\s+)?"
    r"(?P<a>[\w:.]+)\s+(?:calls?\s+)?with\s+(?P<b>[\w:.]+)\s*$",
    "replace_api",
    lambda m: {"from": m["a"], "to": m["b"]},
)
_rule(
    r"^\s*add\s+(?:an?\s+)?import\s+(?P<name>\w+)\s+from\s+(?P<module>[\w.]+)\s*$",
    "add_import",
    lambda m: {"module": m["module"], "name": m["name"]},
)
_rule(
    r"^\s*add\s+(?:an?\s+)?import\s+(?P<module>[\w.]+)\s*$",
    "add_import",
    lambda m: {"module": m["module"], "name": None},
)
_rule(
    r"^\s*(?:remove|drop)\s+unused\s+imports\s*$",
    "remove_unused_imports",
    lambda m: {},
)
_rule(
    r"^\s*(?:organize|sort)\s+imports\s*$",
    "organize_imports",
    lambda m: {},
)
_rule(
    r"^\s*add\s+(?:the\s+)?(?:#\s*)?include\s+(?P<hdr>[<\"][^>\"]+[>\"])\s+(?:to\s+)?(?P<file>\S+)\s*$",
    "add_include",
    lambda m: {"file": m["file"], "include": m["hdr"].strip("<>\""), "system": m["hdr"].startswith("<")},
)
_rule(
    r"^\s*move\s+(?P<sym>\w+)\s+from\s+(?P<from>\S+)\s+to\s+(?P<to>\S+)\s*$",
    "move_symbol",
    lambda m: {"symbol": m["sym"], "from_file": m["from"], "to_file": m["to"]},
)
_rule(
    r"^\s*remove\s+dead\s+code\s*$",
    "remove_dead_code",
    lambda m: {},
)
_rule(
    r"^\s*inline\s+(?:the\s+)?(?:function\s+)?(?P<sym>[\w:.]+)\s*$",
    "inline_function",
    lambda m: {"symbol": m["sym"]},
)
_rule(
    r"^\s*change\s+(?:the\s+)?return\s+type\s+of\s+(?P<sym>[\w:.]+)\s+(?:to|->)\s+(?P<t>[\w:<>,*&\s]+?)\s*$",
    "change_return_type",
    lambda m: {"symbol": m["sym"], "new_type": m["t"].strip()},
)
_rule(
    r"^\s*change\s+(?:the\s+)?(?:type\s+of\s+)?parameter\s+(?P<p>\w+)\s+of\s+(?P<sym>[\w:.]+)\s+(?:to|->)\s+(?P<t>[\w:<>,*&\s]+?)\s*$",
    "change_parameter_type",
    lambda m: {"symbol": m["sym"], "parameter": m["p"], "new_type": m["t"].strip()},
)
_rule(
    r"^\s*change\s+(?:the\s+)?type\s+of\s+(?P<sym>[\w:.]+)\s+(?:to|->)\s+(?P<t>[\w:<>,*&\s]+?)\s*$",
    "change_type",
    lambda m: {"symbol": m["sym"], "new_type": m["t"].strip()},
)
_rule(
    r"^\s*forward[- ]declare\s+(?P<sym>[\w:.]+)\s+in\s+(?P<file>\S+)\s*$",
    "forward_declare",
    lambda m: {"symbol": m["sym"], "file": m["file"]},
)


_UNDERSTOOD = [
    "rename <symbol> to <name>",
    "rename parameter <old> of <symbol> to <new>",
    "replace <old_api> with <new_api>",
    "add import <name> from <module>  /  add import <module>",
    "remove unused imports  /  organize imports",
    "add include <header> to <file>",
    "move <symbol> from <file> to <file>",
    "inline <function>",
    "remove dead code",
    "change return type of <symbol> to <type>  (C++)",
    "change type of parameter <p> of <symbol> to <type>  (C++)",
    "change type of <symbol> to <type>  (C++)",
    "forward-declare <symbol> in <file>  (C++)",
]


def parse_intent(text: str) -> dict[str, Any]:
    """Map `text` to `{op, params, confidence}` — or `{op: "unknown", understood: [...]}`."""
    for pattern, op, build in _RULES:
        match = pattern.match(text)
        if match:
            return {"op": op, "params": build(match), "confidence": "high"}
    return {"op": "unknown", "understood": _UNDERSTOOD}


__all__ = ["parse_intent"]
