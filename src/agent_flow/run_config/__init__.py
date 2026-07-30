"""Run configuration — HOW and WHERE a flow runs, as opposed to what it IS.

models.py   RunConfig / NodeRunConfig, the settings model and its precedence
sources.py  the --config layer: file or inline JSON, deep-merged
params.py   run params — the domain values the pipeline runs on
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_flow.run_config.models import RUNTIME_PARAM_KEY, NodeRunConfig, RunConfig, runtime_param, runtime_param_fields
from agent_flow.run_config.params import normalize_run_config, parse_params, validate_params


def build_run_config(
    config_file: str | Path | list[str] | None = None,
    base: "dict[str, Any] | RunConfig | None" = None,
    **cli_overrides: Any,
) -> RunConfig:
    """Construct a RunConfig from the source stack, honoring the precedence chain.

    Args:
        config_file: optional `--config` source(s). Each is a file path OR inline
            JSON (`{...}`); a list is deep-merged in order (later wins).
        base: the programmatic `run_config=` — a dict or a RunConfig — the
            pipeline author's own defaults, the LOWEST explicit source (below
            --config).
        **cli_overrides: generic settings set on the CLI. A None value means "not
            set on the CLI" and is dropped so a lower-priority source (env / .env
            / --config / base / default) wins — else a None init kwarg would clobber it.

    Precedence (first wins): CLI > env (AGENT_FLOW_*) > .env > --config > base > default.
    """
    init_kwargs = {k: v for k, v in cli_overrides.items() if v is not None}
    return RunConfig(_config_sources=config_file, _base=normalize_run_config(base), **init_kwargs)


__all__ = [
    "RUNTIME_PARAM_KEY",
    "NodeRunConfig",
    "RunConfig",
    "build_run_config",
    "normalize_run_config",
    "parse_params",
    "runtime_param",
    "runtime_param_fields",
    "validate_params",
]
