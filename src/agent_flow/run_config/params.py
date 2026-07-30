"""Run PARAMS — the domain values a pipeline runs on.

Params are the third layer beside the flow (what the pipeline IS) and the run
config (HOW and WHERE it runs): WHAT it runs on. Open by default; a flow may
name a params model to type them.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from agent_flow.run_config.models import RunConfig


def validate_params(model: type | None, values: Mapping[str, Any]) -> dict[str, Any]:
    """Validate domain params against a flow's params model; return a str dict.

    The PURE core of param resolution, shared by both entry points: the CLI wraps
    it to pretty-print a ValidationError and exit 2, while the programmatic
    `run_flow` lets the ValidationError propagate to its caller (a library must
    raise, not exit). `model is None` -> pass the values through untyped.

    Values are dumped in JSON mode and stringified because `params` are the
    `{name}` templating bag (all strings); None becomes "".
    """
    if model is None:
        return dict(values)
    obj = model(**values)
    return {k: ("" if v is None else str(v)) for k, v in obj.model_dump(mode="json").items()}


def normalize_run_config(run_config: "dict[str, Any] | RunConfig | None") -> dict[str, Any] | None:
    """Coerce a programmatic run_config= (dict OR RunConfig) to a plain dict, or None.

    A RunConfig instance is dumped with defaults EXCLUDED, so it contributes only
    the settings the caller actually set — it behaves as a base layer, not a wall
    of defaults that would shadow env/.env.
    """
    if run_config is None:
        return None
    if isinstance(run_config, RunConfig):
        return run_config.model_dump(exclude_defaults=True)
    return dict(run_config)


def parse_params(items: list[str] | None) -> dict[str, str]:
    """Parse repeatable `KEY=VALUE` CLI strings into a params dict.

    Values stay strings (they feed a domain settings model and {name} templating).
    `KEY=` yields "". A missing `=` raises ValueError (so `--param foo` fails
    loudly).
    """
    out: dict[str, str] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--param must be KEY=VALUE, got {item!r}")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"--param has empty key: {item!r}")
        out[key] = value
    return out
