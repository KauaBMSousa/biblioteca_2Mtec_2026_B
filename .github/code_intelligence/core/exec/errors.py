"""Exceptions for `core.exec`: infrastructure failures, never expected outcomes.

A failed build or a failing test is data (`{"status": "FAILED", ...}`), not
an error -- a caller asking "did the tests pass" expects the answer might
be no. These exceptions are for the cases a caller cannot productively
branch on without fixing the environment first: the tool needed to answer
the question is not there, or nothing here declares a toolchain this
module knows how to drive. `TimeoutError` (builtin) is reused for the third
infrastructure case -- the subprocess exceeded its budget -- rather than
adding a fourth exception type for something the standard library already
names correctly.

Every exception raised anywhere under `core.exec` surfaces to an MCP tool
caller as `{"error": {"type": "<ThisClassName>", "message": ...}}` for
free, via `client.WorkspaceClient._call_in_process`'s generic
`except Exception` normalization -- no separate error-code plumbing needed
per tool.
"""


class ToolNotInstalledError(RuntimeError):
    """A required external tool is not on PATH (or not importable) here."""


class UnsupportedLanguageError(RuntimeError):
    """No known build/test/lint/format toolchain marker was found for this project."""


__all__ = ["ToolNotInstalledError", "UnsupportedLanguageError"]
