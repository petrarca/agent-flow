"""Assembling the `--config` layer — file/inline sources, merged deepest-last.

`--config` is repeatable and accepts either a path or inline JSON. Each source
is parsed, key-checked against the model, and deep-merged over the previous one
so a later source overrides a single nested key without replacing its whole
section.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


def _is_inline_json(value: str) -> bool:
    """True when a --config value is inline JSON (starts with `{` or `[`) vs a file path.

    A leading `[` is included so an inline array is parsed and then rejected by the
    mapping check — a clearer error than treating it as a (missing) file path.
    """
    stripped = value.strip()
    return stripped.startswith("{") or stripped.startswith("[")


def _load_config_source(value: str) -> dict[str, Any]:
    """Read one --config value — inline JSON `{...}` or a YAML/JSON file path — to a dict."""

    if _is_inline_json(value):
        data = json.loads(value)
        origin = "inline config"
    else:
        path = Path(value)
        if not path.exists():
            raise FileNotFoundError(f"run config file not found: {value}")
        data = yaml.safe_load(path.read_text())
        origin = f"run config {value}"
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{origin} must be a mapping at the top level")
    return data


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge `overlay` onto `base`; dict values merge, scalars/lists replace.

    Deep so a patch tweaks ONE key of a dict-valued setting — e.g.
    `{"durations": {"long": 900}}` over `{"durations": {"short": 60, "long": 600}}`
    keeps `short` — rather than replacing the whole `durations` map. A per-node
    entry under `nodes:` merges the same way, one node at a time.
    """
    merged = dict(base)
    for key, value in overlay.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _deep_merge(existing, value)
        else:
            merged[key] = value
    return merged


def _validate_config_keys(data: dict[str, Any], allowed: set[str]) -> dict[str, Any]:
    """Reject unknown top-level keys (a typo). Returns the dict unchanged for chaining.

    Shared by the --config assembly and the programmatic run_config= base, so both
    fail loudly on the same typo. `allowed` is passed in rather than read off
    RunConfig here: this module must not depend on the model it feeds, or the two
    would import each other.
    """
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown run config keys {sorted(unknown)} (allowed: {sorted(allowed)})")
    return data


def _assemble_config(sources: list[str], allowed: set[str]) -> dict[str, Any]:
    """Load each --config source in order and deep-merge them (later wins).

    Validates the top-level keys of the MERGED result once (so a typo in any
    layer fails loudly). Returns the merged dict, ready to feed the settings model.
    """
    merged: dict[str, Any] = {}
    for value in sources:
        merged = _deep_merge(merged, _load_config_source(value))
    return _validate_config_keys(merged, allowed)
