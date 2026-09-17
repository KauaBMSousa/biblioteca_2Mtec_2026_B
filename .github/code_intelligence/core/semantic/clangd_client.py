"""A minimal synchronous LSP client for `clangd` (ENHANCEME §V, option 1).

clangd already resolves C++ the way a compiler does; over LSP it will hand
back a `WorkspaceEdit` for a rename or an exact reference list, across
files, without this project shipping a LLVM-linked tool. This client
speaks just enough of the protocol for that: `initialize`, `didOpen`,
`textDocument/rename`, `textDocument/references`, `textDocument/prepareRename`.

It is deliberately blocking and short-lived — spun up for one operation,
torn down after. Cross-file answers need clangd's background index; the
client waits (bounded) for the first indexing pass to settle and reports
`index_settled: False` when it had to give up waiting.
"""

import json
import os
import select
import subprocess
import time
from pathlib import Path
from typing import Any

from code_intelligence.core.semantic.cpp_clang import find_compilation_database


class ClangdUnavailable(RuntimeError):
    """clangd is not on PATH, or the workspace has no compilation database."""


def _uri(path: Path) -> str:
    return "file://" + str(path)


def _path_from_uri(uri: str) -> str:
    return uri[len("file://") :] if uri.startswith("file://") else uri


class ClangdClient:
    """One clangd subprocess, driven request/response over stdio."""

    def __init__(self, root: Path, index_wait: float = 25.0) -> None:
        self.root = Path(root)
        self.index_wait = index_wait
        self._proc: subprocess.Popen | None = None
        self._id = 0
        self._buf = b""
        self._indexing = False
        self._saw_indexing = False
        self._open_file: str | None = None

    # -- lifecycle -------------------------------------------------------

    def __enter__(self) -> "ClangdClient":
        import shutil

        if shutil.which("clangd") is None:
            raise ClangdUnavailable("clangd is not on PATH")
        database = find_compilation_database(self.root)
        if database is None:
            raise ClangdUnavailable("no compile_commands.json found; clangd cannot resolve C++")

        self._proc = subprocess.Popen(
            [
                "clangd",
                f"--compile-commands-dir={database.parent}",
                "--background-index",
                "--pch-storage=memory",
                "--log=error",
                "--header-insertion=never",
            ],
            cwd=str(self.root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        self._fd = self._proc.stdout.fileno()
        try:
            self._initialize()
        except BaseException:
            self._proc.kill()
            self._proc = None
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        if self._proc is None:
            return
        try:
            self._request("shutdown", None, timeout=5)
            self._notify("exit", None)
        except Exception:  # noqa: BLE001 - best-effort teardown
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            self._proc.kill()

    # -- wire ----------------------------------------------------------

    def _write(self, message: dict[str, Any]) -> None:
        body = json.dumps(message).encode("utf-8")
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
        assert self._proc and self._proc.stdin
        self._proc.stdin.write(header + body)
        self._proc.stdin.flush()

    def _fill(self, deadline: float) -> bool:
        """Read one chunk from clangd into `self._buf`; False on timeout or EOF."""
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        ready, _, _ = select.select([self._fd], [], [], remaining)
        if not ready:
            return False
        chunk = os.read(self._fd, 65536)
        if not chunk:
            return False
        self._buf += chunk
        return True

    def _read_message(self, deadline: float) -> dict[str, Any] | None:
        while b"\r\n\r\n" not in self._buf:
            if not self._fill(deadline):
                return None
        header, _, rest = self._buf.partition(b"\r\n\r\n")
        length = 0
        for line in header.split(b"\r\n"):
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":")[1].strip())
        self._buf = rest
        while len(self._buf) < length:
            if not self._fill(deadline):
                return None
        message = self._buf[:length]
        self._buf = self._buf[length:]
        return json.loads(message)

    def _pump(self, want_id: int | None, timeout: float) -> Any:
        """Read messages until the response for `want_id` arrives (or timeout)."""
        deadline = time.monotonic() + timeout
        while True:
            message = self._read_message(deadline)
            if message is None:
                raise TimeoutError(f"clangd did not respond within {timeout}s")
            if "id" in message and message.get("method") is None:
                if want_id is not None and message["id"] == want_id:
                    if "error" in message:
                        raise RuntimeError(f"clangd error: {message['error']}")
                    return message.get("result")
                continue
            method = message.get("method")
            if method == "$/progress":
                value = (message.get("params") or {}).get("value") or {}
                title = value.get("title", "")
                if value.get("kind") == "begin" and "index" in title.lower():
                    self._indexing = True
                    self._saw_indexing = True
                elif value.get("kind") == "end":
                    self._indexing = False
            elif method in ("workspace/configuration", "client/registerCapability"):
                self._write({"jsonrpc": "2.0", "id": message["id"], "result": None})

    def _next_id(self) -> int:
        self._id += 1
        return self._id

    def _request(self, method: str, params: Any, timeout: float = 30.0) -> Any:
        request_id = self._next_id()
        msg = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)
        return self._pump(request_id, timeout)

    def _notify(self, method: str, params: Any) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    # -- protocol ----------------------------------------------------

    def _initialize(self) -> None:
        self._request(
            "initialize",
            {
                "processId": os.getpid(),
                "rootUri": _uri(self.root),
                "capabilities": {
                    "textDocument": {
                        "rename": {"prepareSupport": True},
                        "references": {},
                    },
                    "window": {"workDoneProgress": True},
                },
            },
            timeout=30,
        )
        self._notify("initialized", {})

    def did_open(self, relpath: str) -> None:
        path = self.root / relpath
        text = path.read_text(encoding="utf-8", errors="replace")
        self._open_file = relpath
        self._notify(
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": _uri(path),
                    "languageId": "cpp",
                    "version": 1,
                    "text": text,
                }
            },
        )

    def _drain(self, seconds: float) -> None:
        """Read and process any messages that arrive within `seconds` (progress, etc.)."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            message = self._read_message(deadline)
            if message is None:
                return
            method = message.get("method")
            if method == "$/progress":
                value = (message.get("params") or {}).get("value") or {}
                if value.get("kind") == "begin" and "index" in value.get("title", "").lower():
                    self._indexing = True
                    self._saw_indexing = True
                elif value.get("kind") == "end":
                    self._indexing = False
            elif method in ("workspace/configuration", "client/registerCapability") and "id" in message:
                self._write({"jsonrpc": "2.0", "id": message["id"], "result": None})

    def wait_for_index(self) -> bool:
        """Get the main file's AST ready, then briefly wait out any background indexing.

        The `documentSymbol` round trip blocks until clangd has parsed the
        open file — enough for a same-TU rename. Cross-file coverage needs
        the background index; this waits for its first pass to end, capped
        at `index_wait`. Returns False if that cap was hit while indexing
        was still running.
        """
        if self._open_file is None:
            return False
        try:
            self._request(
                "textDocument/documentSymbol",
                {"textDocument": {"uri": _uri(self.root / self._open_file)}},
                timeout=30,
            )
        except (TimeoutError, RuntimeError):
            pass

        deadline = time.monotonic() + self.index_wait
        started = time.monotonic()
        while time.monotonic() < deadline:
            self._drain(1.0)
            if self._saw_indexing and not self._indexing:
                return True
            if not self._saw_indexing and time.monotonic() - started > 4.0:
                return True  # small or already-warm project: no indexing observed
        return not self._indexing

    def rename(self, relpath: str, line0: int, char0: int, new_name: str) -> dict[str, Any] | None:
        path = self.root / relpath
        return self._request(
            "textDocument/rename",
            {
                "textDocument": {"uri": _uri(path)},
                "position": {"line": line0, "character": char0},
                "newName": new_name,
            },
            timeout=60,
        )

    def references(
        self, relpath: str, line0: int, char0: int, include_declaration: bool = True
    ) -> list[dict[str, Any]]:
        path = self.root / relpath
        result = self._request(
            "textDocument/references",
            {
                "textDocument": {"uri": _uri(path)},
                "position": {"line": line0, "character": char0},
                "context": {"includeDeclaration": include_declaration},
            },
            timeout=60,
        )
        return result or []


def workspace_edit_to_changes(edit: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Normalize a `WorkspaceEdit` to `{abs_path: [TextEdit, ...]}`."""
    out: dict[str, list[dict[str, Any]]] = {}
    if not edit:
        return out
    if "changes" in edit and edit["changes"]:
        for uri, edits in edit["changes"].items():
            out.setdefault(_path_from_uri(uri), []).extend(edits)
    for doc_change in edit.get("documentChanges") or []:
        if "textDocument" in doc_change and "edits" in doc_change:
            uri = doc_change["textDocument"]["uri"]
            out.setdefault(_path_from_uri(uri), []).extend(doc_change["edits"])
    return out


def apply_text_edits(text: str, edits: list[dict[str, Any]]) -> str:
    """Apply LSP `TextEdit`s (0-based line/character ranges) to `text`."""
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def pos(p: dict[str, int]) -> int:
        line = min(p["line"], len(lines))
        return offsets[line] + p["character"] if line < len(offsets) else len(text)

    spans = sorted(
        ((pos(e["range"]["start"]), pos(e["range"]["end"]), e["newText"]) for e in edits),
        key=lambda s: s[0],
        reverse=True,
    )
    out = text
    for start, end, new_text in spans:
        out = out[:start] + new_text + out[end:]
    return out


__all__ = [
    "ClangdClient",
    "ClangdUnavailable",
    "workspace_edit_to_changes",
    "apply_text_edits",
]
