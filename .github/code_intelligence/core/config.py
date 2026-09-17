"""Loads and merges the tool's configuration.

Configuration is a deep merge of `default_config.json` (shipped with this
package) with an optional repo-level `.code-intelligence.json` override
(successor to the old tool's `.code-quality.json` — same rule-threshold
schema ported forward). Any key omitted from the override falls back to
the default.

Adds `daemon.socket_path`/`daemon.autostart` (reserved for Phase B — never
built or read for daemon logic in Phase A, just accepted so a config file
that already sets them doesn't fail to load), `bootstrap.enabled` and
`context_budget.default_max_lines`.

Ported from `tools/code_quality/code_quality/config.py`.
"""

import copy
import json
from importlib import resources
from pathlib import Path
from typing import Any

#: The repo-level override filename, successor to `.code-quality.json`.
CONFIG_FILENAME = ".code-intelligence.json"


class ConfigError(Exception):
    """Raised when the configured `.code-intelligence.json` file is invalid."""


def load_default_config() -> dict[str, Any]:
    """Load the package's bundled `default_config.json`.

    Returns:
        A fresh dict copy of the default configuration.
    """
    data = resources.files("code_intelligence").joinpath("default_config.json").read_text(
        encoding="utf-8"
    )
    return json.loads(data)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `override` on top of `base`, returning a new dict.

    Dict values are merged key-by-key; any other value type (including
    lists) is replaced wholesale by the override's value.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load_config(config_path: Path | None, root: Path) -> dict[str, Any]:
    """Load the effective configuration for a workspace.

    Args:
        config_path: Explicit config path, or None to auto-detect
            `<root>/.code-intelligence.json`.
        root: The workspace root directory, used for auto-detection.

    Returns:
        The merged configuration dict, with a `"_source"` key recording
        which override file (if any) was applied, or None if only defaults
        were used.

    Raises:
        ConfigError: If an explicit or auto-detected config file exists but
            contains invalid JSON or is not a JSON object.
    """
    default = load_default_config()

    candidate = config_path if config_path is not None else root / CONFIG_FILENAME

    if not candidate.exists():
        if config_path is not None:
            raise ConfigError(f"Config file not found: {candidate}")
        default["_source"] = None
        return default

    try:
        override = json.loads(candidate.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"Invalid JSON in config file {candidate}: {exc}") from exc

    if not isinstance(override, dict):
        raise ConfigError(f"Config file {candidate} must contain a JSON object")

    merged = _deep_merge(default, override)
    merged["_source"] = str(candidate)
    return merged


def compute_config_hash(config: dict) -> str:
    """Sha256 of the merged effective config (sorted-key JSON), excluding `_source`.

    Feeds the index's `config_hash` cache-invalidation dimension: a config
    edit invalidates every stored row even though no file's own content
    changed.
    """
    import hashlib

    payload = {key: value for key, value in config.items() if key != "_source"}
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
