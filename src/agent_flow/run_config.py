"""Run configuration — the CLI's own settings, as a pydantic-settings model.

A pipeline run needs two kinds of input:

  - GENERIC run settings the library understands: runtime, run_dir, agent_dir,
    instructions, llm_concurrency, show_events. THIS module owns them, as the
    `RunConfig` settings model below.
  - DOMAIN params (arbitrary, consumer-defined): e.g. product_key, repos_root.
    Those live in a SEPARATE, flow-supplied settings model (see run_cli's
    `params_model`); the library attaches no meaning to them. They are threaded
    to pipeline(**params) and used for {name} templating in inputs/context/paths.

`RunConfig` is a `pydantic_settings.BaseSettings`. It resolves values from,
in decreasing precedence:

  1. CLI overrides      — init kwargs from the CLI flags (build_run_config drops
                          None so an unset flag does not clobber lower sources)
  2. environment        — AGENT_FLOW_* variables (e.g. AGENT_FLOW_RUNTIME)
  3. .env file          — same AGENT_FLOW_* names
  4. YAML --config file  — generic keys at the top level (a `params:` section is
                          ignored here; the domain model reads it)
  5. field defaults

This precedence is expressed via `settings_customise_sources`. The generic
settings use the `AGENT_FLOW_` env prefix so they never collide with a flow's
bare-named domain params (product_key, product_repos_root, …).

Access follows the house pattern (as in coco-rag / sonnet-server): an lru_cache
singleton via `get_settings()`, installed by `init_settings(...)`, reset in tests
by `clear_settings()`. `build_run_config(...)` is the pure constructor.

There is deliberately no "product" (or any domain) concept here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
    YamlConfigSettingsSource,
)

from agent_flow.agent_runtime import DEFAULT_IDLE_TIMEOUT_S


def _validate_yaml_top_level(path: Path) -> None:
    """Reject unknown top-level keys in a --config YAML (fail loudly on typos).

    The allowed keys are the RunConfig fields themselves (derived from the model,
    so this never drifts as fields are added) plus `params` (the domain section).
    """
    import yaml

    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"run config {path} must be a mapping at the top level")
    allowed = set(RunConfig.model_fields) | {"params"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"{path}: unknown config keys {sorted(unknown)} (allowed: {sorted(RunConfig.model_fields)} + params)")


class RunConfig(BaseSettings):
    """Resolved generic run configuration (the library's own CLI settings).

    Domain params are NOT here — they belong to a flow-supplied settings model.
    Env vars use the AGENT_FLOW_ prefix (e.g. AGENT_FLOW_RUN_DIR).

    Pass a YAML config path via the `_config_file` init kwarg (the CLI does this
    for `--config`); it is read as the lowest-priority source below defaults-from
    -code but above field defaults.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_FLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    runtime: str = Field(default="opencode", description="Agent runtime: 'opencode' (real) or 'mock' (no-token stub).")
    run_dir: str = Field(
        default="",
        description="Control-sidecar + relative-artifact root. Supports {param} templating; empty -> a fresh temp dir per run.",
    )
    agent_dir: str = Field(default="", description="Where agent definitions live (opencode --dir); becomes the subprocess cwd.")
    instructions: str = Field(default="", description="Run-wide brief injected into every agent prompt (inline text).")
    instructions_file: str = Field(default="", description="Path to a file whose content is the run-wide brief (wins over `instructions`).")
    llm_concurrency: int | None = Field(default=None, description="Max concurrent LLM agents; None -> engine default.")
    show_events: bool = Field(default=False, description="Stream live agent events to the console.")
    show_diffs: bool = Field(default=False, description="Render file-change diffs (edit/write) as blocks. Composes with show_events.")
    diff_style: str = Field(default="unified", description="Diff layout when show_diffs is on: 'unified' (one column) or 'split' (side-by-side).")
    model: str = Field(
        default="",
        description="Model (provider/model) for every node. Empty -> the runtime resolves it from its own config. Per-node model overrides.",
    )
    idle_timeout_s: int = Field(
        default=DEFAULT_IDLE_TIMEOUT_S,
        description="Liveness timeout (s): kill an agent only after this long with no event/sidecar. Per-node idle_timeout_s overrides.",
    )

    # The YAML --config path for the current construction. Stashed on the class
    # by __init__ so the settings_customise_sources CLASSMETHOD (which sees only
    # settings_cls, not the instance) can build a source for it. Not thread-safe
    # by construction, which is fine: the CLI builds a RunConfig once per process.
    _af_config_file: str | Path | None = None

    def __init__(self, _config_file: str | Path | None = None, **kwargs: Any) -> None:
        # Validate the YAML's top-level keys up front (loud failure on typos),
        # then stash the path for settings_customise_sources and build.
        if _config_file:
            _validate_yaml_top_level(Path(_config_file))
        type(self)._af_config_file = _config_file
        try:
            super().__init__(**kwargs)
        finally:
            type(self)._af_config_file = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        config_file = getattr(settings_cls, "_af_config_file", None)
        # Precedence (first wins): CLI/init > env > .env > YAML config > defaults.
        sources: tuple[PydanticBaseSettingsSource, ...] = (init_settings, env_settings, dotenv_settings)
        if config_file:
            sources = (*sources, YamlConfigSettingsSource(settings_cls, yaml_file=config_file))
        return sources

    def resolved_instructions(self) -> str:
        """The run-wide brief: instructions_file content if given, else instructions."""
        if self.instructions_file:
            return Path(self.instructions_file).read_text()
        return self.instructions


def build_run_config(config_file: str | Path | None = None, **cli_overrides: Any) -> RunConfig:
    """Construct a RunConfig from the source stack, honoring the precedence chain.

    Args:
        config_file: optional YAML `--config` path (lowest explicit source).
        **cli_overrides: generic settings set on the CLI. A None value means "not
            set on the CLI" and is dropped so a lower-priority source (env / .env
            / YAML / default) wins — otherwise a None init kwarg would clobber it.

    Precedence (first wins): CLI overrides > env (AGENT_FLOW_*) > .env > YAML > default.
    """
    init_kwargs = {k: v for k, v in cli_overrides.items() if v is not None}
    return RunConfig(_config_file=config_file, **init_kwargs)


# --- Global settings lifecycle (house pattern: lru_cache singleton) ---------

_settings_override: RunConfig | None = None


def init_settings(config_file: str | Path | None = None, **cli_overrides: Any) -> RunConfig:
    """Build the RunConfig and install it as the process-wide singleton."""
    global _settings_override
    _settings_override = build_run_config(config_file, **cli_overrides)
    _get_settings_cached.cache_clear()
    return get_settings()


def get_settings() -> RunConfig:
    """Return the process-wide RunConfig (cached; built from env/.env if not init'd)."""
    return _get_settings_cached()


@lru_cache(maxsize=1)
def _get_settings_cached() -> RunConfig:
    return _settings_override if _settings_override is not None else RunConfig()


def clear_settings() -> None:
    """Reset the settings singleton — for testing only."""
    global _settings_override
    _settings_override = None
    _get_settings_cached.cache_clear()


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
