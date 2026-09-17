"""The Context Hierarchy (L1-L6) + Context Budget — the core token-reduction API."""

from code_intelligence.core.context.context import (
    find_dependencies,
    find_dependents,
    find_symbol_by_name,
    get_agent_context,
    get_context,
    get_file_structure,
    get_symbol,
    get_violations,
    get_workspace_structure,
    search_symbols,
    symbol_to_dict,
)

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
