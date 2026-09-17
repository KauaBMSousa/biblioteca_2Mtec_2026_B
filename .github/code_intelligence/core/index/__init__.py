"""The persistent, incremental, symbol-oriented SQLite index — Phase A's central new piece."""

from code_intelligence.core.index.indexer import Indexer, IndexStats, compute_parser_version
from code_intelligence.core.index.schema import SCHEMA_VERSION
from code_intelligence.core.index.store import IndexStore

__all__ = ["Indexer", "IndexStats", "compute_parser_version", "IndexStore", "SCHEMA_VERSION"]
