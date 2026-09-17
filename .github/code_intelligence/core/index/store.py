"""Open/close/transaction lifecycle and row-level CRUD for the SQLite index.

`IndexStore` is the only module that runs raw SQL — everything above it
(`core/index/indexer.py`, `core/workspace/workspace.py`) works with plain
dicts/dataclasses. This keeps the schema's actual shape (see `schema.py`)
swappable without touching indexing/query logic.
"""

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from code_intelligence.core.index.schema import SCHEMA_VERSION, create_schema, drop_schema


class IndexStore:
    """Owns one SQLite connection to `<root>/.code-intelligence/index.sqlite3`."""

    def __init__(self, db_path: Path) -> None:
        """Prepare a store bound to `db_path`; call `open()` before use."""
        self.db_path = db_path
        self._connection: sqlite3.Connection | None = None

    @property
    def connection(self) -> sqlite3.Connection:
        """The live connection; raises if `open()` hasn't been called."""
        if self._connection is None:
            raise RuntimeError("IndexStore is not open — call open() first")
        return self._connection

    def open(self) -> None:
        """Open the SQLite connection, creating the schema (or rebuilding it) as needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: the daemon (daemon/server.py) serves each
        # connection on its own thread, all sharing one `Workspace`/IndexStore.
        # Concurrent access is still serialized by the daemon's own dispatch
        # lock — this only lifts sqlite3's same-thread guard, it does not by
        # itself make concurrent use safe.
        connection = sqlite3.connect(str(self.db_path), check_same_thread=False)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        self._connection = connection

        stored_version = self._meta_get_raw("schema_version")
        if stored_version != str(SCHEMA_VERSION):
            drop_schema(connection)
            create_schema(connection)
            self._meta_set_raw("schema_version", str(SCHEMA_VERSION))
            connection.commit()
        else:
            create_schema(connection)  # idempotent; ensures tables exist on a partial DB

    def close(self) -> None:
        """Close the connection, if open."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None

    def __enter__(self) -> "IndexStore":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block of writes atomically; rolls back on any exception."""
        connection = self.connection
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    # -- meta ---------------------------------------------------------

    def _meta_get_raw(self, key: str) -> str | None:
        try:
            row = self.connection.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        except sqlite3.OperationalError:
            return None  # table doesn't exist yet (first-ever open)
        return row["value"] if row else None

    def _meta_set_raw(self, key: str, value: str) -> None:
        self.connection.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def meta_get(self, key: str) -> str | None:
        """Read one `meta` key's value, or None if unset."""
        return self._meta_get_raw(key)

    def meta_set(self, key: str, value: str) -> None:
        """Write one `meta` key's value and commit immediately."""
        self._meta_set_raw(key, value)
        self.connection.commit()

    # -- revisions (cheap stale-state detection; ENHANCEME §O) ------------

    def get_revision(self, name: str) -> int:
        """Read one monotonic revision counter (0 when never bumped).

        Three names are in use: `content` (bumped by every local edit and
        by the indexer whenever it reparses or drops a file — i.e. whenever
        the code on disk moved), `indexed` (set equal to `content` at the
        end of every index pass — so `content == indexed` means the
        structural index is in step with disk), and `semantic` (bumped
        whenever `refresh_semantic` re-extracted at least one unit).
        """
        raw = self._meta_get_raw(f"revision:{name}")
        return int(raw) if raw else 0

    def bump_revision(self, name: str, *, commit: bool = False) -> int:
        """Increment one revision counter; returns its new value."""
        new_value = self.get_revision(name) + 1
        self._meta_set_raw(f"revision:{name}", str(new_value))
        if commit:
            self.connection.commit()
        return new_value

    def set_revision(self, name: str, value: int, *, commit: bool = False) -> None:
        """Force one revision counter to `value` (used to mark `indexed` caught up to `content`)."""
        self._meta_set_raw(f"revision:{name}", str(value))
        if commit:
            self.connection.commit()

    # -- answer cache (ENHANCEME §N) -----------------------------------

    def answer_cache_get(
        self, key: str, revision_content: int, revision_semantic: int
    ) -> str | None:
        """The cached `value_json` for `key`, only if it was computed at the current revisions."""
        row = self.connection.execute(
            "SELECT value_json FROM answer_cache "
            "WHERE key = ? AND revision_content = ? AND revision_semantic = ?",
            (key, revision_content, revision_semantic),
        ).fetchone()
        return row["value_json"] if row else None

    def answer_cache_put(
        self, key: str, revision_content: int, revision_semantic: int, value_json: str
    ) -> None:
        """Store one computed answer, replacing any older entry for the same `key`."""
        import time as _time

        self.connection.execute(
            "INSERT INTO answer_cache (key, revision_content, revision_semantic, value_json, created_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
            "revision_content = excluded.revision_content, "
            "revision_semantic = excluded.revision_semantic, "
            "value_json = excluded.value_json, created_at = excluded.created_at",
            (key, revision_content, revision_semantic, value_json,
             _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())),
        )
        self.connection.commit()

    def answer_cache_prune(self, revision_content: int) -> None:
        """Drop entries older than the current content revision (best effort)."""
        self.connection.execute(
            "DELETE FROM answer_cache WHERE revision_content < ?", (revision_content,)
        )
        self.connection.commit()

    # -- plans + diagnostic context (ENHANCEME §4/§8/§10) --------------

    def plan_put(
        self, plan_id: str, intent: str, op: str, spec_json: str,
        revision_content: int, estimate_json: str,
    ) -> None:
        import time as _time

        self.connection.execute(
            "INSERT INTO plans (plan_id, intent, op, spec_json, revision_content, estimate_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(plan_id) DO UPDATE SET "
            "intent=excluded.intent, op=excluded.op, spec_json=excluded.spec_json, "
            "revision_content=excluded.revision_content, estimate_json=excluded.estimate_json, "
            "created_at=excluded.created_at",
            (plan_id, intent, op, spec_json, revision_content, estimate_json,
             _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())),
        )
        self.connection.commit()

    def plan_get(self, plan_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM plans WHERE plan_id = ?", (plan_id,)
        ).fetchone()
        return dict(row) if row else None

    def diagnostic_context_replace(self, entries: list[dict[str, Any]]) -> None:
        """Replace the stored diagnose() entries with `entries` (each {diagnostic_id, kind, ...})."""
        import time as _time

        now = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        self.connection.execute("DELETE FROM diagnostic_context")
        for e in entries:
            self.connection.execute(
                "INSERT INTO diagnostic_context (diagnostic_id, kind, file, line, symbol, message, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (e["diagnostic_id"], e["kind"], e.get("file"), e.get("line"),
                 e.get("symbol"), e.get("message"), now),
            )
        self.connection.commit()

    def diagnostic_context_get(self, diagnostic_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM diagnostic_context WHERE diagnostic_id = ?", (diagnostic_id,)
        ).fetchone()
        return dict(row) if row else None

    # -- persistent task state (ENHANCEME §10) ------------------------

    def task_create(self, intent: str, scope: str | None) -> str:
        """Allocate the next `T<n>` id, record it as `running`, and return it."""
        import time as _time

        n = self.get_revision("task_seq") + 1
        self.set_revision("task_seq", n)
        task_id = f"T{n}"
        now = _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())
        self.connection.execute(
            "INSERT INTO tasks (task_id, intent, scope, status, summary_json, created_at, updated_at) "
            "VALUES (?, ?, ?, 'running', NULL, ?, ?)",
            (task_id, intent, scope, now, now),
        )
        self.connection.commit()
        return task_id

    def task_finish(self, task_id: str, status: str, summary_json: str) -> None:
        import time as _time

        self.connection.execute(
            "UPDATE tasks SET status = ?, summary_json = ?, updated_at = ? WHERE task_id = ?",
            (status, summary_json, _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()), task_id),
        )
        self.connection.commit()

    def task_get(self, task_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    def task_list(self, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- files ----------------------------------------------------------

    def get_file(self, path: str) -> dict[str, Any] | None:
        """Return one file's stored row as a dict, or None if not indexed."""
        row = self.connection.execute("SELECT * FROM files WHERE path = ?", (path,)).fetchone()
        return dict(row) if row else None

    def list_file_paths(self) -> list[str]:
        """Return every indexed file's path."""
        rows = self.connection.execute("SELECT path FROM files").fetchall()
        return [row["path"] for row in rows]

    def list_files(self) -> list[dict[str, Any]]:
        """Return every indexed file's full row, as dicts."""
        rows = self.connection.execute("SELECT * FROM files ORDER BY path").fetchall()
        return [dict(row) for row in rows]

    def upsert_file(self, row: dict[str, Any]) -> None:
        """Insert or replace one file's row (does not commit — call within a transaction)."""
        columns = list(row.keys())
        placeholders = ", ".join("?" for _ in columns)
        column_list = ", ".join(columns)
        updates = ", ".join(f"{col} = excluded.{col}" for col in columns if col != "path")
        self.connection.execute(
            f"INSERT INTO files ({column_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(path) DO UPDATE SET {updates}",
            [row[col] for col in columns],
        )

    def delete_file(self, path: str) -> None:
        """Delete one file's row (cascades to symbols/diagnostics/imports/symbol_extra)."""
        self.connection.execute("DELETE FROM files WHERE path = ?", (path,))

    # -- symbols ----------------------------------------------------------

    def delete_symbols_for_file(self, path: str) -> None:
        """Delete every symbol (and its dependent rows) belonging to one file."""
        self.connection.execute("DELETE FROM symbols WHERE file_path = ?", (path,))

    def insert_symbol(self, row: dict[str, Any]) -> None:
        """Insert one symbol row (does not commit)."""
        columns = list(row.keys())
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO symbols ({', '.join(columns)}) VALUES ({placeholders})",
            [row[col] for col in columns],
        )

    def get_symbol(self, symbol_id: str) -> dict[str, Any] | None:
        """Return one symbol's stored row as a dict, or None if not found."""
        row = self.connection.execute("SELECT * FROM symbols WHERE symbol_id = ?", (symbol_id,)).fetchone()
        return dict(row) if row else None

    def list_symbols_for_file(self, path: str) -> list[dict[str, Any]]:
        """Return every symbol defined in one file, in declaration (start_line) order."""
        rows = self.connection.execute(
            "SELECT * FROM symbols WHERE file_path = ? ORDER BY start_line", (path,)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_all_symbols(self) -> list[dict[str, Any]]:
        """Return every symbol in the index."""
        rows = self.connection.execute("SELECT * FROM symbols ORDER BY file_path, start_line").fetchall()
        return [dict(row) for row in rows]

    def find_symbols_by_name(self, name: str) -> list[dict[str, Any]]:
        """Return every symbol whose `name` or `qualified_name` equals `name`."""
        rows = self.connection.execute(
            "SELECT * FROM symbols WHERE name = ? OR qualified_name = ?", (name, name)
        ).fetchall()
        return [dict(row) for row in rows]

    def search_symbols(self, query: str, limit: int = 50) -> list[dict[str, Any]]:
        """Case-insensitive substring search over `name`/`qualified_name`, using the
        `idx_symbols_name` index (an indexed prefix/equality lookup on `name`, unioned
        with a substring scan) — backs `workspace/symbol` (fixme §27) without an O(files)
        per-file enumeration.
        """
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        rows = self.connection.execute(
            "SELECT * FROM symbols WHERE (name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\') "
            "ORDER BY name COLLATE NOCASE LIMIT ?",
            (pattern, pattern, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    # -- symbol_extra (deviation; see schema.py) --------------------------

    def upsert_symbol_extra(self, symbol_id: str, token_stream: list, references: list) -> None:
        """Store one symbol's token stream + reference identifiers for cross-file passes."""
        self.connection.execute(
            "INSERT INTO symbol_extra (symbol_id, token_stream_json, references_json) VALUES (?, ?, ?) "
            "ON CONFLICT(symbol_id) DO UPDATE SET token_stream_json = excluded.token_stream_json, "
            "references_json = excluded.references_json",
            (symbol_id, json.dumps(token_stream), json.dumps(references)),
        )

    def get_symbol_extra(self, symbol_id: str) -> dict[str, Any] | None:
        """Return `{token_stream, references}` for one symbol, or None if absent."""
        row = self.connection.execute(
            "SELECT token_stream_json, references_json FROM symbol_extra WHERE symbol_id = ?", (symbol_id,)
        ).fetchone()
        if row is None:
            return None
        return {
            "token_stream": [tuple(item) for item in json.loads(row["token_stream_json"])],
            "references": json.loads(row["references_json"]),
        }

    # -- diagnostics --------------------------------------------------------

    def delete_diagnostics_for_file(self, path: str) -> None:
        """Delete every diagnostic attributed to one file."""
        self.connection.execute("DELETE FROM diagnostics WHERE file_path = ?", (path,))

    def delete_diagnostics_by_rule(self, rule: str) -> None:
        """Delete every diagnostic with a given rule code (used for a full duplication re-run)."""
        self.connection.execute("DELETE FROM diagnostics WHERE rule = ?", (rule,))

    def delete_diagnostics_by_rule_prefix(self, prefix: str) -> None:
        """Delete every diagnostic whose rule code starts with `prefix` (e.g. `CPPCHECK_`)."""
        self.connection.execute(
            "DELETE FROM diagnostics WHERE rule LIKE ? ESCAPE '\\'",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        )

    def has_diagnostics_with_rule_prefix(self, prefix: str) -> bool:
        """Whether any diagnostic's rule code starts with `prefix`."""
        row = self.connection.execute(
            "SELECT 1 FROM diagnostics WHERE rule LIKE ? ESCAPE '\\' LIMIT 1",
            (prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%",),
        ).fetchone()
        return row is not None

    def insert_diagnostic(self, row: dict[str, Any]) -> None:
        """Insert one diagnostic row (does not commit)."""
        columns = list(row.keys())
        placeholders = ", ".join("?" for _ in columns)
        self.connection.execute(
            f"INSERT INTO diagnostics ({', '.join(columns)}) VALUES ({placeholders})",
            [row[col] for col in columns],
        )

    def list_diagnostics(
        self, *, severity_min: int | None = None, file_path: str | None = None
    ) -> list[dict[str, Any]]:
        """Return diagnostics, optionally filtered by minimum severity and/or file."""
        clauses = []
        params: list[Any] = []
        if severity_min is not None:
            clauses.append("severity >= ?")
            params.append(severity_min)
        if file_path is not None:
            clauses.append("file_path = ?")
            params.append(file_path)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.connection.execute(
            f"SELECT * FROM diagnostics{where} ORDER BY file_path, start_line", params
        ).fetchall()
        return [dict(row) for row in rows]

    def list_diagnostics_for_symbol(self, symbol_id: str) -> list[dict[str, Any]]:
        """Return every diagnostic attributed to one symbol."""
        rows = self.connection.execute(
            "SELECT * FROM diagnostics WHERE symbol_id = ? ORDER BY start_line", (symbol_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- dependencies ----------------------------------------------------

    def delete_all_dependencies(self) -> None:
        """Delete every stored call-graph edge (used before a full re-resolve)."""
        self.connection.execute("DELETE FROM dependencies")

    def insert_dependency(self, from_symbol_id: str, to_symbol_id: str, kind: str, confidence: str) -> None:
        """Insert one call-graph edge (does not commit)."""
        self.connection.execute(
            "INSERT INTO dependencies (from_symbol_id, to_symbol_id, kind, confidence) VALUES (?, ?, ?, ?)",
            (from_symbol_id, to_symbol_id, kind, confidence),
        )

    def list_dependencies_from(self, symbol_id: str) -> list[dict[str, Any]]:
        """Return every edge originating at `symbol_id` (its `calls`/`references`)."""
        rows = self.connection.execute(
            "SELECT * FROM dependencies WHERE from_symbol_id = ?", (symbol_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    def list_dependencies_to(self, symbol_id: str) -> list[dict[str, Any]]:
        """Return every edge targeting `symbol_id` (its `called_by`/dependents)."""
        rows = self.connection.execute(
            "SELECT * FROM dependencies WHERE to_symbol_id = ?", (symbol_id,)
        ).fetchall()
        return [dict(row) for row in rows]

    # -- imports -----------------------------------------------------------

    def delete_imports_for_file(self, path: str) -> None:
        """Delete every import row belonging to one file."""
        self.connection.execute("DELETE FROM imports WHERE file_path = ?", (path,))

    def insert_import(self, file_path: str, module: str, line: int) -> None:
        """Insert one import row (does not commit)."""
        self.connection.execute(
            "INSERT INTO imports (file_path, module, line) VALUES (?, ?, ?)", (file_path, module, line)
        )

    def list_imports_for_file(self, path: str) -> list[dict[str, Any]]:
        """Return every import statement in one file."""
        rows = self.connection.execute(
            "SELECT * FROM imports WHERE file_path = ? ORDER BY line", (path,)
        ).fetchall()
        return [dict(row) for row in rows]


__all__ = ["IndexStore"]
