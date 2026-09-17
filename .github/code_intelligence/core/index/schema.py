"""SQLite DDL for the persistent, incremental, symbol-oriented index.

Tables match the plan's "Persistent incremental index" section:
`meta(key, value)`, `files(path PK, ...)`, `symbols(symbol_id PK, ...)`,
`diagnostics(id PK, ...)`, `dependencies(from_symbol_id, to_symbol_id, ...)`,
`semantic_edges(...)` + `semantic_units(...)` + `semantic_unit_deps(...)` for
exactly-resolved references from per-language backends,
`imports(file_path, module, line)` — plus indexes on `symbols(file_path)`,
`symbols(name)`, `diagnostics(severity)`, `diagnostics(file_path)`.

Two auxiliary tables beyond the plan's literal column list — `symbol_extra`
(each function/method symbol's normalized token stream + call/instantiation
reference identifiers) — are a deliberate, documented deviation: without
them, the cross-file passes (`DUPLICATE_BLOCK` detection, call-graph
resolution) would have to re-parse every *unchanged* file's full AST on
every `index()` call just to get their token streams/reference lists,
defeating the incremental design's central promise. Storing this small
amount of extra structural data keeps parsing itself properly incremental
(the expensive step) while still letting project-wide passes run without
touching the filesystem for unchanged files. See `core/index/indexer.py`
and the Phase A final report for the full rationale.
"""

import sqlite3

#: Bumped whenever this DDL changes in a way that requires a full rebuild
#: (a mismatch against the stored `meta.schema_version` value triggers a
#: `DROP TABLE IF EXISTS` + recreate in `store.py`, not a silent partial
#: migration).
SCHEMA_VERSION = 3  # 3: + to_usr on edges, semantic_definitions

_DDL = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS files (
    path                 TEXT PRIMARY KEY,
    language             TEXT NOT NULL,
    content_hash         TEXT NOT NULL,
    ast_hash             TEXT NOT NULL,
    physical_lines       INTEGER NOT NULL,
    logical_code_lines   INTEGER NOT NULL,
    comment_lines        INTEGER NOT NULL,
    blank_lines          INTEGER NOT NULL,
    parse_ok             INTEGER NOT NULL,
    parse_fallback_used  INTEGER NOT NULL,
    parse_error          TEXT,
    parse_confidence     TEXT NOT NULL,
    is_generated         INTEGER NOT NULL DEFAULT 0,
    is_test_file         INTEGER NOT NULL DEFAULT 0,
    is_config_file       INTEGER NOT NULL DEFAULT 0,
    parser_version        TEXT NOT NULL,
    config_hash            TEXT NOT NULL,
    indexed_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS symbols (
    symbol_id            TEXT PRIMARY KEY,
    file_path            TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    kind                 TEXT NOT NULL,
    name                 TEXT NOT NULL,
    qualified_name       TEXT NOT NULL,
    namespace            TEXT,
    start_line           INTEGER NOT NULL,
    start_column         INTEGER NOT NULL,
    end_line             INTEGER NOT NULL,
    end_column           INTEGER NOT NULL,
    body_start_line      INTEGER NOT NULL,
    body_end_line        INTEGER NOT NULL,
    loc                  INTEGER NOT NULL,
    cyclomatic_complexity INTEGER,
    has_doc              INTEGER NOT NULL,
    content_hash         TEXT NOT NULL,
    confidence           TEXT NOT NULL DEFAULT 'high'
);

CREATE INDEX IF NOT EXISTS idx_symbols_file_path ON symbols(file_path);
CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);

-- Deviation from the plan's literal column list (see module docstring):
-- per-symbol token stream + reference identifiers, needed so duplication
-- detection and call-graph resolution never re-parse an unchanged file.
CREATE TABLE IF NOT EXISTS symbol_extra (
    symbol_id         TEXT PRIMARY KEY REFERENCES symbols(symbol_id) ON DELETE CASCADE,
    token_stream_json TEXT NOT NULL,
    references_json   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS diagnostics (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol_id     TEXT REFERENCES symbols(symbol_id) ON DELETE CASCADE,
    file_path     TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    rule          TEXT NOT NULL,
    severity      INTEGER NOT NULL,
    confidence    TEXT NOT NULL DEFAULT 'high',
    start_line    INTEGER NOT NULL,
    end_line      INTEGER NOT NULL,
    start_column  INTEGER,
    end_column    INTEGER,
    value         TEXT,
    threshold     TEXT,
    message       TEXT NOT NULL,
    detail_json   TEXT
);

CREATE INDEX IF NOT EXISTS idx_diagnostics_severity ON diagnostics(severity);
CREATE INDEX IF NOT EXISTS idx_diagnostics_file_path ON diagnostics(file_path);
CREATE INDEX IF NOT EXISTS idx_diagnostics_symbol_id ON diagnostics(symbol_id);

CREATE TABLE IF NOT EXISTS dependencies (
    from_symbol_id TEXT NOT NULL,
    to_symbol_id   TEXT NOT NULL,
    kind           TEXT NOT NULL,
    confidence     TEXT NOT NULL DEFAULT 'medium'
);

CREATE INDEX IF NOT EXISTS idx_dependencies_from ON dependencies(from_symbol_id);
CREATE INDEX IF NOT EXISTS idx_dependencies_to ON dependencies(to_symbol_id);

-- Exactly-resolved references, produced by a per-language semantic backend
-- (clangd, jedi, tsc, OpenRewrite, php-parser). Kept SEPARATE from
-- `dependencies`, which holds name-matched guesses: merging the two would
-- lose the only property that makes these worth computing -- that they are
-- known to be right.
CREATE TABLE IF NOT EXISTS semantic_edges (
    from_file   TEXT NOT NULL,
    from_line   INTEGER NOT NULL,
    from_column INTEGER NOT NULL,
    to_name     TEXT NOT NULL,
    to_file     TEXT,
    to_line     INTEGER,
    -- clang's Unified Symbol Resolution: the same string for a symbol in
    -- every translation unit. A C++ call site sees the header declaration
    -- while the index holds the .cpp definition, so file:line cannot match
    -- them and this can. Null for backends whose resolution already points
    -- at the definition (jedi, tsc).
    to_usr      TEXT,
    kind        TEXT NOT NULL DEFAULT 'call',
    backend     TEXT NOT NULL,
    language    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_semantic_edges_from ON semantic_edges(from_file);
CREATE INDEX IF NOT EXISTS idx_semantic_edges_to ON semantic_edges(to_file, to_line);
CREATE INDEX IF NOT EXISTS idx_semantic_edges_name ON semantic_edges(to_name);
CREATE INDEX IF NOT EXISTS idx_semantic_edges_usr ON semantic_edges(to_usr);

-- Where each USR is DEFINED, as seen while parsing a unit. This is the
-- join that turns an index symbol (a .cpp definition) into the USR its
-- callers reference through a header.
CREATE TABLE IF NOT EXISTS semantic_definitions (
    usr      TEXT NOT NULL,
    file     TEXT NOT NULL,
    line     INTEGER NOT NULL,
    name     TEXT NOT NULL,
    unit     TEXT NOT NULL,
    language TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_semantic_definitions_loc ON semantic_definitions(file, line);
CREATE INDEX IF NOT EXISTS idx_semantic_definitions_usr ON semantic_definitions(usr);

-- What each analyzed unit READ. A C++ translation unit's include set, a
-- module's imports: editing any of these makes the unit's semantic edges
-- stale even though the unit's own text did not change. Without this the
-- index would serve exact-looking edges that quietly stopped being true.
CREATE TABLE IF NOT EXISTS semantic_unit_deps (
    unit       TEXT NOT NULL,
    depends_on TEXT NOT NULL,
    language   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_semantic_unit_deps_unit ON semantic_unit_deps(unit);
CREATE INDEX IF NOT EXISTS idx_semantic_unit_deps_dep ON semantic_unit_deps(depends_on);

-- Per-unit bookkeeping: the content hash the edges were extracted from, and
-- whether extraction succeeded. A unit that FAILED to analyze is recorded as
-- such, so "no edges" can be told apart from "not analyzable".
CREATE TABLE IF NOT EXISTS semantic_units (
    unit        TEXT PRIMARY KEY,
    language    TEXT NOT NULL,
    backend     TEXT NOT NULL,
    unit_hash   TEXT NOT NULL,
    ok          INTEGER NOT NULL DEFAULT 1,
    error       TEXT,
    extracted_at TEXT NOT NULL
);

-- ENHANCEME §N: cache of whole computed answers (impact, affected_tests,
-- find_dependents, ...), keyed by query + the index/semantic revision the
-- answer was computed at. A row whose revisions no longer match the
-- current ones is simply ignored (and opportunistically pruned) -- never
-- migrated. This is not part of the index's correctness surface, so it is
-- added here rather than behind a SCHEMA_VERSION bump.
CREATE TABLE IF NOT EXISTS answer_cache (
    key               TEXT PRIMARY KEY,
    revision_content  INTEGER NOT NULL,
    revision_semantic INTEGER NOT NULL,
    value_json        TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

-- ENHANCEME §4/§10: a resolved plan awaiting execute(plan_id). Keyed by
-- the content revision it was resolved at, so execute() can detect a
-- workspace that moved underneath it.
CREATE TABLE IF NOT EXISTS plans (
    plan_id          TEXT PRIMARY KEY,
    intent           TEXT NOT NULL,
    op               TEXT NOT NULL,
    spec_json        TEXT NOT NULL,
    revision_content INTEGER NOT NULL,
    estimate_json    TEXT NOT NULL,
    created_at       TEXT NOT NULL
);

-- ENHANCEME §8: the last diagnose() run's entries, addressable by a stable
-- id so failure_context(id) can return the minimum context for one.
CREATE TABLE IF NOT EXISTS diagnostic_context (
    diagnostic_id TEXT PRIMARY KEY,
    kind          TEXT NOT NULL,
    file          TEXT,
    line          INTEGER,
    symbol        TEXT,
    message       TEXT,
    created_at    TEXT NOT NULL
);

-- ENHANCEME §10: persistent task state. Every task() run records a row,
-- addressable as `T<n>`, so a later session can `resume task T183` -- the
-- resume re-derives plan+execute fresh from the stored intent rather than
-- trusting a stale plan.
CREATE TABLE IF NOT EXISTS tasks (
    task_id      TEXT PRIMARY KEY,
    intent       TEXT NOT NULL,
    scope        TEXT,
    status       TEXT NOT NULL,
    summary_json TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS imports (
    file_path TEXT NOT NULL REFERENCES files(path) ON DELETE CASCADE,
    module    TEXT NOT NULL,
    line      INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_imports_file_path ON imports(file_path);
"""


def create_schema(connection: sqlite3.Connection) -> None:
    """Create every table/index if they don't already exist."""
    connection.executescript(_DDL)
    connection.commit()


def drop_schema(connection: sqlite3.Connection) -> None:
    """Drop every table (used when `meta.schema_version` is stale — a full rebuild)."""
    connection.executescript(
        """
        DROP TABLE IF EXISTS imports;
        DROP TABLE IF EXISTS answer_cache;
        DROP TABLE IF EXISTS plans;
        DROP TABLE IF EXISTS diagnostic_context;
        DROP TABLE IF EXISTS tasks;
        DROP TABLE IF EXISTS dependencies;
        DROP TABLE IF EXISTS semantic_edges;
        DROP TABLE IF EXISTS semantic_unit_deps;
        DROP TABLE IF EXISTS semantic_units;
        DROP TABLE IF EXISTS semantic_definitions;
        DROP TABLE IF EXISTS diagnostics;
        DROP TABLE IF EXISTS symbol_extra;
        DROP TABLE IF EXISTS symbols;
        DROP TABLE IF EXISTS files;
        DROP TABLE IF EXISTS meta;
        """
    )
    connection.commit()


__all__ = ["SCHEMA_VERSION", "create_schema", "drop_schema"]
