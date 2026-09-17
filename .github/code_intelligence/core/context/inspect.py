"""One adaptive read: `inspect(target, detail)` (ENHANCEME §13).

A single entry point for "show me X" that returns the *least* that answers
the question and never source unless asked. `target` selects what:

* `{"path": "src/foo.py"}`      -> the file's symbol structure
* `{"symbol": "Foo.bar"}`       -> that symbol's metadata (+ location)
* `{"range": {"file","start","end"}}` -> the overlapping symbol(s)
* `{"diagnostic": "d1a2b3c4"}`  -> `failure_context` for that diagnostic

`detail="source"` adds the exact source text for the resolved symbol /
range / file. Anything else stays structural.
"""

from typing import Any


def inspect(workspace: Any, target: dict[str, Any], detail: str | None = None) -> dict[str, Any]:
    want_source = detail == "source"

    if "diagnostic" in target:
        from code_intelligence.core.context import planner

        return planner.failure_context(workspace, target["diagnostic"], detail)

    if "path" in target:
        path = target["path"]
        structure = workspace.get_file_structure(path)
        if structure is None:
            raise LookupError(f"{path!r} is not indexed")
        out: dict[str, Any] = {"kind": "file", "path": path, "structure": structure}
        if want_source:
            file_row = workspace.store.get_file(path)
            end = file_row["physical_lines"] if file_row else 1_000_000
            out["source"] = workspace.read_range(path, 1, end).get("content")
        return out

    if "range" in target:
        r = target["range"]
        rng = workspace.read_range(r["file"], r["start"], r["end"])
        out = {
            "kind": "range",
            "file": r["file"],
            "range": [r["start"], r["end"]],
            "symbol_id": rng.get("symbol_id"),
            "content_hash": rng.get("content_hash"),
        }
        if want_source:
            out["source"] = rng.get("content")
        return out

    if "symbol" in target:
        row = workspace.find_symbol(target["symbol"], target.get("file"))
        if row is None:
            raise LookupError(f"symbol {target['symbol']!r} is not in the index")
        out = {"kind": "symbol", "symbol": row}
        if want_source:
            loc = row["location"]
            out["source"] = workspace.read_range(
                row["file"], loc["start_line"], loc["end_line"]
            ).get("content")
        return out

    raise ValueError(
        "inspect target must be one of {path}, {symbol}, {range}, {diagnostic}"
    )


__all__ = ["inspect"]
