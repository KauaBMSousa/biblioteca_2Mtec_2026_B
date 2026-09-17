"""The 7-level Context Hierarchy + the Context Budget mechanism.

fixme §14/§15/§16 — the literal "reduce tokens" deliverable. Every level is
a separately-callable, increasingly expensive query:

    L1 get_workspace_structure()   — per-file summary, whole workspace
    L2 get_file_structure(path)    — symbols in one file, no source content
    L3 get_symbol(symbol_id)       — one symbol's full metadata, no content
    L4/5 get_context(symbol_id)    — one symbol's source content, budgeted
    L6 find_dependents/find_dependencies — call-graph neighbors

`get_context`'s budget (`max_lines`/`max_bytes`) is a hard non-silent-
truncation contract: exceeding it returns `{"truncated": true,
"available_range": {...}, "recommended_ranges": [...]}` — it never returns
a silently-clipped `content` string.

`get_agent_context()` composes a severity-floor diagnostics query with the
existing per-rule context-sizing table (ported from
`tools/code_quality/code_quality/agent_context.py`).
"""

import json
from pathlib import Path
from typing import Any

from code_intelligence.core.diagnostics.violation import Severity
from code_intelligence.core.hashes.content_hash import content_hash
from code_intelligence.core.index.store import IndexStore
from code_intelligence.core.workspace.security import PathTraversalError, resolve_within_workspace

#: Fixed padding used for the "everything else" fallback row of the sizing
#: table — deliberately not the same knob as `agent_context.lines_before`/
#: `lines_after` (whose default is 5, for NAMING_VIOLATION specifically).
_GENERIC_PADDING = 15
_DOCSTRING_PADDING = 10

#: Caps for `get_agent_context`, overridable per workspace under
#: `agent_context` in the config.
#:
#: This call advertises itself as "the single cheapest call for what needs
#: attention right now", but it used to emit EVERY finding at/above
#: `min_severity` with no ceiling. On a real C++ workspace (419 findings at
#: HIGH+) that is 320KB / ~10,900 lines — it overflowed an MCP client's
#: token budget and returned nothing usable, which is the opposite of its
#: purpose. The caps below keep the payload bounded; the `summary` block it
#: now returns carries the FULL per-rule and per-severity counts, so the
#: shape of the whole problem still arrives even when the list is trimmed.
_DEFAULT_MAX_VIOLATIONS = 40
_DEFAULT_MAX_CONTEXT_ENTRIES = 40
#: PROCEDURAL_MONOLITH embeds an index of the file's functions; one such
#: entry reached 3.4KB (60 functions) on its own.
_DEFAULT_MAX_FUNCTION_INDEX = 25


# -- shape helpers (index row -> public dict) --------------------------------


def _diagnostic_to_dict(row: dict[str, Any]) -> dict[str, Any]:
    """Serialize one `diagnostics` row into the public violation shape."""
    return {
        "code": row["rule"],
        "severity": Severity(row["severity"]).name,
        "file": row["file_path"],
        "line": row["start_line"],
        "end_line": row["end_line"],
        "column": row["start_column"],
        "end_column": row["end_column"],
        "message": row["message"],
        "detail": json.loads(row["detail_json"]) if row["detail_json"] else None,
        "symbol_id": row["symbol_id"],
        "confidence": row["confidence"],
    }


def symbol_to_dict(store: IndexStore, row: dict[str, Any]) -> dict[str, Any]:
    """Build the full L3 `get_symbol` payload for one symbol row: metadata, no source content."""
    calls = [edge["to_symbol_id"] for edge in store.list_dependencies_from(row["symbol_id"]) if edge["kind"] == "call"]
    references = [
        edge["to_symbol_id"] for edge in store.list_dependencies_from(row["symbol_id"]) if edge["kind"] == "reference"
    ]
    called_by = [edge["from_symbol_id"] for edge in store.list_dependencies_to(row["symbol_id"])]
    call_graph_confidence = "low" if any(
        edge["confidence"] == "low" for edge in store.list_dependencies_from(row["symbol_id"])
    ) else "medium"
    violations = [diag["rule"] for diag in store.list_diagnostics_for_symbol(row["symbol_id"])]

    return {
        "symbol_id": row["symbol_id"],
        "kind": row["kind"],
        "name": row["name"],
        "qualified_name": row["qualified_name"],
        "file": row["file_path"],
        "namespace": row["namespace"],
        "location": {
            "start_line": row["start_line"],
            "end_line": row["end_line"],
            "start_column": row["start_column"],
            "end_column": row["end_column"],
            "body_start_line": row["body_start_line"],
            "body_end_line": row["body_end_line"],
        },
        "metrics": {"loc": row["loc"], "cyclomatic_complexity": row["cyclomatic_complexity"]},
        "has_doc": bool(row["has_doc"]),
        "calls": calls,
        "called_by": called_by,
        "references": references,
        "call_graph_confidence": call_graph_confidence,
        "violations": violations,
        "content_hash": row["content_hash"],
        "confidence": row["confidence"],
    }


# -- L1: workspace structure --------------------------------------------------


def get_workspace_structure(store: IndexStore) -> dict[str, Any]:
    """L1: a per-file summary of the whole workspace — no symbol detail, no content."""
    files = store.list_files()
    entries = []
    for file_row in files:
        symbols = store.list_symbols_for_file(file_row["path"])
        diagnostics = store.list_diagnostics(file_path=file_row["path"])
        entries.append(
            {
                "path": file_row["path"],
                "language": file_row["language"],
                "physical_lines": file_row["physical_lines"],
                "symbol_count": len(symbols),
                "violation_count": len(diagnostics),
                "parse_confidence": file_row["parse_confidence"],
            }
        )
    return {"files": entries, "file_count": len(entries)}


# -- L2: file structure --------------------------------------------------------


def get_file_structure(store: IndexStore, path: str) -> dict[str, Any] | None:
    """L2: every symbol defined in one file — kind/name/location/metrics only, no content."""
    file_row = store.get_file(path)
    if file_row is None:
        return None
    symbols = store.list_symbols_for_file(path)
    return {
        "path": path,
        "language": file_row["language"],
        "physical_lines": file_row["physical_lines"],
        "symbols": [
            {
                "symbol_id": row["symbol_id"],
                "kind": row["kind"],
                "name": row["name"],
                "qualified_name": row["qualified_name"],
                "start_line": row["start_line"],
                "end_line": row["end_line"],
                "has_doc": bool(row["has_doc"]),
                "loc": row["loc"],
                "cyclomatic_complexity": row["cyclomatic_complexity"],
            }
            for row in symbols
        ],
    }


# -- L3: one symbol --------------------------------------------------------


def get_symbol(store: IndexStore, symbol_id: str) -> dict[str, Any] | None:
    """L3: one symbol's full metadata (location, metrics, calls, violations) — no content."""
    row = store.get_symbol(symbol_id)
    if row is None:
        return None
    return symbol_to_dict(store, row)


def find_symbol_by_name(store: IndexStore, name: str, file: str | None = None) -> dict[str, Any] | None:
    """Resolve a bare name/qualified_name (optionally scoped to `file`) to a symbol_id row."""
    candidates = store.find_symbols_by_name(name)
    if file is not None:
        candidates = [row for row in candidates if row["file_path"] == file]
    return candidates[0] if candidates else None


def search_symbols(store: IndexStore, query: str, limit: int = 50) -> list[dict[str, Any]]:
    """Indexed substring search over every symbol's name/qualified_name — backs `workspace/symbol`."""
    return [symbol_to_dict(store, row) for row in store.search_symbols(query, limit)]


# -- L4/5: symbol content, budgeted -----------------------------------------


def _chunk_ranges(start_line: int, end_line: int, max_lines: int) -> list[dict[str, int]]:
    """Split `[start_line, end_line]` into `max_lines`-sized chunks for `recommended_ranges`."""
    ranges = []
    cursor = start_line
    while cursor <= end_line:
        chunk_end = min(cursor + max_lines - 1, end_line)
        ranges.append({"start_line": cursor, "end_line": chunk_end})
        cursor = chunk_end + 1
    return ranges


def get_context(
    store: IndexStore, root: Path, symbol_id: str, max_lines: int | None = None, max_bytes: int | None = None
) -> dict[str, Any] | None:
    """L4/5: one symbol's exact source content, honoring the Context Budget.

    Never silently truncates: when the symbol's full `[start_line, end_line]`
    span exceeds `max_lines`/`max_bytes`, returns `{"truncated": true,
    "available_range": {...}, "recommended_ranges": [...]}` instead of a
    clipped `content` string.
    """
    row = store.get_symbol(symbol_id)
    if row is None:
        return None

    try:
        file_path = resolve_within_workspace(root, row["file_path"])
    except PathTraversalError:
        return None
    try:
        lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None

    start_line, end_line = row["start_line"], row["end_line"]
    span_lines = lines[start_line - 1 : end_line]
    span_text = "\n".join(span_lines)
    line_count = len(span_lines)
    byte_count = len(span_text.encode("utf-8"))

    exceeds_lines = max_lines is not None and line_count > max_lines
    exceeds_bytes = max_bytes is not None and byte_count > max_bytes

    if exceeds_lines or exceeds_bytes:
        if max_lines is not None:
            chunk_size = max_lines
        elif max_bytes is not None and line_count > 0:
            avg_bytes_per_line = max(1, byte_count // line_count)
            chunk_size = max(1, max_bytes // avg_bytes_per_line)
        else:
            chunk_size = line_count
        return {
            "symbol_id": symbol_id,
            "file": row["file_path"],
            "truncated": True,
            "available_range": {"start_line": start_line, "end_line": end_line},
            "recommended_ranges": _chunk_ranges(start_line, end_line, chunk_size),
            "line_count": line_count,
            "byte_count": byte_count,
        }

    return {
        "symbol_id": symbol_id,
        "file": row["file_path"],
        "truncated": False,
        "start_line": start_line,
        "end_line": end_line,
        "content": span_text,
        "content_hash": content_hash(span_text),
        "line_count": line_count,
        "byte_count": byte_count,
    }


# -- L6: dependency graph neighbors ------------------------------------------


def find_dependents(store: IndexStore, symbol_id: str) -> list[dict[str, Any]]:
    """L6: every symbol that calls/references `symbol_id` (its dependents/callers).

    Prefers EXACT edges when a semantic backend has covered the symbol's
    file, and says so: each row carries `resolution` -- `"exact"` when a
    real compiler resolved the call, `"heuristic"` when it came from name
    matching over the tree-sitter index.

    The distinction is not cosmetic. Name matching answered "0 callers" for
    a C++ method that `main()` plainly calls, because the call site writes
    `run` while the symbol is stored `AutoencoderRunner::run`; an agent
    reads that as dead code. The exact path resolves the receiver's type
    and finds it.
    """
    from code_intelligence.core.index import semantic_store

    row = store.get_symbol(symbol_id)
    if row is not None:
        exact = semantic_store.references_to(store, row["file_path"], row["start_line"])
        if exact:
            results = []
            for edge in exact:
                caller = _enclosing_symbol_of(store, edge["from_file"], edge["from_line"])
                results.append(
                    {
                        "symbol_id": caller["symbol_id"] if caller else None,
                        "qualified_name": (caller["qualified_name"] or caller["name"]) if caller else None,
                        "file": edge["from_file"],
                        "line": edge["from_line"],
                        "kind": edge["kind"],
                        "resolution": "exact",
                        "backend": edge["backend"],
                    }
                )
            return results

    edges = store.list_dependencies_to(symbol_id)
    results = []
    for edge in edges:
        caller = store.get_symbol(edge["from_symbol_id"])
        if caller is not None:
            results.append(
                {
                    "symbol_id": caller["symbol_id"],
                    "qualified_name": caller["qualified_name"],
                    "file": caller["file_path"],
                    "kind": edge["kind"],
                    "resolution": "heuristic",
                }
            )
    return results


def _enclosing_symbol_of(store: IndexStore, file: str, line: int) -> dict[str, Any] | None:
    """The indexed symbol whose span contains `line` in `file`.

    An exact edge knows the call site's position, not which function it sits
    in; the structural index knows that. Using both is what makes the answer
    readable ("called by X at file:line") instead of a bare coordinate.
    """
    best: dict[str, Any] | None = None
    for candidate in store.list_symbols_for_file(file):
        start, end = candidate["start_line"], candidate["end_line"]
        if start is None or end is None or not (start <= line <= end):
            continue
        if best is None or candidate["start_line"] >= best["start_line"]:
            best = candidate
    return best


def find_dependencies(store: IndexStore, symbol_id: str) -> list[dict[str, Any]]:
    """L6: every symbol `symbol_id` calls/references (its dependencies/callees)."""
    edges = store.list_dependencies_from(symbol_id)
    results = []
    for edge in edges:
        callee = store.get_symbol(edge["to_symbol_id"])
        if callee is not None:
            results.append(
                {
                    "symbol_id": callee["symbol_id"],
                    "qualified_name": callee["qualified_name"],
                    "file": callee["file_path"],
                    "kind": edge["kind"],
                    "confidence": edge["confidence"],
                }
            )
    return results


# -- get_violations ------------------------------------------------------------


def get_violations(store: IndexStore, severity: str | None = None) -> list[dict[str, Any]]:
    """Every diagnostic at/above `severity` (default: every diagnostic)."""
    severity_min = Severity[severity].value if severity else None
    rows = store.list_diagnostics(severity_min=severity_min)
    return [_diagnostic_to_dict(row) for row in rows]


# -- get_agent_context (ported from agent_context.py) -------------------------


def _class_span_for(store: IndexStore, file_path: str, name: str, start_line: int) -> dict[str, Any] | None:
    """Find a class/type symbol matching `name`/`start_line` in `file_path`."""
    for row in store.list_symbols_for_file(file_path):
        if row["name"] == name and row["start_line"] == start_line and row["kind"] not in ("function", "method", "constructor", "destructor"):
            return row
    return None


def _context_entries(
    violation: dict[str, Any],
    store: IndexStore,
    lines_before: int,
    lines_after: int,
    max_function_index: int = _DEFAULT_MAX_FUNCTION_INDEX,
) -> list[dict[str, Any]]:
    """Return the `recommended_context` entries for one violation, per the sizing table."""
    detail = violation.get("detail") or {}
    code = violation["code"]

    if code == "NAMING_VIOLATION":
        return [
            {
                "file": violation["file"],
                "start_line": max(1, violation["line"] - lines_before),
                "end_line": violation["line"] + lines_after,
            }
        ]
    if code == "MISSING_DOCSTRING":
        return [
            {
                "file": violation["file"],
                "start_line": max(1, violation["line"] - _DOCSTRING_PADDING),
                "end_line": violation["line"] + _DOCSTRING_PADDING,
            }
        ]
    if code.startswith("FUNCTION_LENGTH_"):
        return [{"file": violation["file"], "start_line": violation["line"], "end_line": violation["end_line"]}]
    if code == "MULTIPLE_TYPE_DEFINITION":
        entries = []
        for entry in detail.get("types", []):
            row = _class_span_for(store, violation["file"], entry.get("name"), entry.get("line"))
            if row is not None:
                entries.append(
                    {"file": violation["file"], "start_line": row["start_line"], "end_line": row["end_line"], "name": row["name"]}
                )
        return entries or [{"file": violation["file"], "start_line": violation["line"], "end_line": violation["end_line"]}]
    if code == "PROCEDURAL_MONOLITH":
        index = [
            {"name": row["name"], "start_line": row["start_line"], "end_line": row["end_line"]}
            for row in store.list_symbols_for_file(violation["file"])
            if row["kind"] == "function"
        ]
        entry = {
            "file": violation["file"],
            "start_line": violation["line"],
            "end_line": violation["end_line"],
            "function_index": index[:max_function_index],
        }
        if len(index) > max_function_index:
            # Say so rather than silently handing back a short list the
            # caller would read as the file's complete set of functions.
            entry["function_index_total"] = len(index)
            entry["function_index_truncated"] = True
        return [entry]
    if code == "DUPLICATE_BLOCK":
        members = detail.get("members", [])
        return [
            {"file": member["file"], "start_line": member["start_line"], "end_line": member["end_line"], "function": member.get("function")}
            for member in members
        ] or [{"file": violation["file"], "start_line": violation["line"], "end_line": violation["end_line"]}]

    return [
        {
            "file": violation["file"],
            "start_line": max(1, violation["line"] - _GENERIC_PADDING),
            "end_line": violation["end_line"] + _GENERIC_PADDING,
        }
    ]


def _value_and_threshold(violation: dict[str, Any], config: dict) -> tuple[Any, Any]:
    """Best-effort extraction of a violation's (value, threshold) pair for critical_violations."""
    detail = violation.get("detail") or {}
    code = violation["code"]
    if code.startswith("LOC_"):
        level = code[len("LOC_"):].lower()
        return detail.get("physical_lines"), config.get("loc_thresholds", {}).get(level)
    if code.startswith("FUNCTION_LENGTH_"):
        level = code[len("FUNCTION_LENGTH_"):].lower()
        return detail.get("physical_lines"), config.get("function_length_thresholds", {}).get(level)
    if code == "NAMING_VIOLATION":
        return len(detail.get("name", "")), detail.get("min_length")
    if code == "PROCEDURAL_MONOLITH":
        return detail.get("cohesion"), None
    if code == "DUPLICATE_BLOCK":
        return detail.get("similarity"), config.get("duplication", {}).get("similarity_threshold")
    return None, None


def _severity_rank(violation: dict[str, Any]) -> int:
    """Numeric severity of a public-shaped violation (higher = worse)."""
    return Severity[violation["severity"]].value


def _diversified_sample(violations: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """At most `limit` violations, worst first, with every rule represented.

    Plain "worst N" is the wrong sample for a summary: on a workspace whose
    findings are 188 DUPLICATE_BLOCK and 86 FUNCTION_LENGTH_CRITICAL, a
    severity-sorted head is all one rule and the reader never learns the
    other kinds exist. So the cap is filled round-robin over the rules,
    taking each rule's worst finding first. The full per-rule counts ride
    along in the payload's `summary`, so nothing is hidden — the list is a
    sample, the counts are complete.
    """
    ordered = sorted(violations, key=lambda v: (-_severity_rank(v), v["file"], v["line"]))
    by_rule: dict[str, list[dict[str, Any]]] = {}
    for violation in ordered:
        by_rule.setdefault(violation["code"], []).append(violation)

    sample: list[dict[str, Any]] = []
    while len(sample) < limit and by_rule:
        for rule in list(by_rule):
            if len(sample) >= limit:
                break
            sample.append(by_rule[rule].pop(0))
            if not by_rule[rule]:
                del by_rule[rule]
    sample.sort(key=lambda v: (-_severity_rank(v), v["file"], v["line"]))
    return sample


def get_agent_context(store: IndexStore, config: dict) -> dict[str, Any]:
    """The `--agent-context`-equivalent minimal, token-economical payload.

    Emits `critical_violations` (a bounded sample of the findings at/above
    `agent_context.min_severity`, stripped to
    file/symbol/rule/location/value/threshold), `recommended_context` (exact
    line ranges worth reading, sized per-rule, for exactly those sampled
    findings) and `summary` (the COMPLETE counts, per rule and per
    severity, plus whether the lists were trimmed).

    The bound is the point. Without it this call emitted every finding at
    or above the floor — 419 of them on a mid-sized C++ workspace, 320KB,
    over any sane token budget — which made "the single cheapest call"
    the most expensive one. `summary.truncated` plus the per-rule counts
    tell the caller what it is not seeing, so a trimmed answer can never be
    mistaken for a complete one; `get_violations` pages through the rest.
    """
    agent_config = config.get("agent_context", {})
    min_severity_name = agent_config.get("min_severity", "HIGH")
    min_severity = Severity[min_severity_name]
    lines_before = agent_config.get("lines_before", 5)
    lines_after = agent_config.get("lines_after", 5)
    max_violations = agent_config.get("max_violations", _DEFAULT_MAX_VIOLATIONS)
    max_context_entries = agent_config.get("max_context_entries", _DEFAULT_MAX_CONTEXT_ENTRIES)
    max_function_index = agent_config.get("max_function_index", _DEFAULT_MAX_FUNCTION_INDEX)

    rows = store.list_diagnostics(severity_min=min_severity.value)
    violations = [_diagnostic_to_dict(row) for row in rows]

    by_rule: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    for violation in violations:
        by_rule[violation["code"]] = by_rule.get(violation["code"], 0) + 1
        by_severity[violation["severity"]] = by_severity.get(violation["severity"], 0) + 1

    sampled = _diversified_sample(violations, max_violations)

    critical_violations = []
    recommended_context: list[dict[str, Any]] = []
    context_total = 0
    for violation in sampled:
        symbol_row = store.get_symbol(violation["symbol_id"]) if violation["symbol_id"] else None
        value, threshold = _value_and_threshold(violation, config)
        critical_violations.append(
            {
                "file": violation["file"],
                "symbol": symbol_row["qualified_name"] if symbol_row else None,
                "symbol_id": violation["symbol_id"],
                "rule": violation["code"],
                "severity": violation["severity"],
                "start_line": violation["line"],
                "end_line": violation["end_line"],
                "value": value,
                "threshold": threshold,
            }
        )
        entries = _context_entries(violation, store, lines_before, lines_after, max_function_index)
        context_total += len(entries)
        if len(recommended_context) < max_context_entries:
            recommended_context.extend(entries[: max_context_entries - len(recommended_context)])

    return {
        "critical_violations": critical_violations,
        "recommended_context": recommended_context,
        "summary": {
            "min_severity": min_severity_name,
            "total_violations": len(violations),
            "returned_violations": len(critical_violations),
            "total_context_entries": context_total,
            "returned_context_entries": len(recommended_context),
            "truncated": len(critical_violations) < len(violations)
            or len(recommended_context) < context_total,
            "by_severity": dict(
                sorted(by_severity.items(), key=lambda kv: -Severity[kv[0]].value)
            ),
            "by_rule": dict(sorted(by_rule.items(), key=lambda kv: -kv[1])),
            "next": "get_violations(severity=..., limit=..., cursor=...) pages through every finding",
        },
    }


__all__ = [
    "get_workspace_structure",
    "get_file_structure",
    "get_symbol",
    "find_symbol_by_name",
    "search_symbols",
    "get_context",
    "find_dependents",
    "find_dependencies",
    "get_violations",
    "get_agent_context",
    "symbol_to_dict",
]
