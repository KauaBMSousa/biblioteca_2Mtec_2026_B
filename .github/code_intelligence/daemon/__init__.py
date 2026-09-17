"""Per-workspace daemon: Unix-socket line-JSON-RPC server over `core.workspace.Workspace`.

`server.py` is the socket loop + file watcher, `protocol.py` is the wire
format and method dispatch table, `lifecycle.py` is `start`/`stop`/`status`
process management via a pidfile. See `code_intelligence/docs/daemon.md`
and `docs/protocol.md`.
"""
