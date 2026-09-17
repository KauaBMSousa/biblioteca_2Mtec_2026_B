"""Defines DiscoveryError, raised for setup-level file-discovery failures.

Ported unchanged from `tools/code_quality/code_quality/discovery_error.py`.
"""


class DiscoveryError(Exception):
    """Raised for setup-level discovery failures (e.g. --changed-only outside git)."""
