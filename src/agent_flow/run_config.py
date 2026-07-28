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
  4. --config sources    — one or more file paths and/or inline JSON, deep-merged
                          in order; generic keys at the top level (a `params:`
                          section is ignored here; the domain model reads it)
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

from collections.abc import Mapping
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)
from pydantic_settings.sources import InitSettingsSource

from agent_flow.core import DEFAULT_IDLE_TIMEOUT_S

# The json_schema_extra KEY that marks a params_model field as runtime-populated
# (set at run time — e.g. by a node's exports — not a user input). Named here so
# both the writer (a consumer's Field) and the reader (runtime_param_fields) share
# one source of truth instead of a hand-typed string.
RUNTIME_PARAM_KEY = "runtime"


class NodeRunConfig(BaseModel):
    """Per-node RUN settings — the shadow of a NodeDef, keyed by node name.

    Everything here is an environment fact (a model string, a path, a timeout),
    NOT a property of the pipeline, so it lives in the run config rather than on
    the portable NodeDef. Each field is None/empty = "not set for this node", so a
    node entry can override just one setting and inherit the rest from the
    run-wide value. Overrides the flow-declared (agent_node) value: this layer is
    "how THIS run behaves", more specific than the flow's standing declaration.
    """

    model_config = {"extra": "forbid"}

    instructions: str = Field(default="", description="Per-node brief, appended LAST to this node's prompt (from config or CLI --instruct).")
    duration: str | None = Field(default=None, description="Duration NAME for this node (overrides the flow-declared duration).")
    idle_timeout_s: int | None = Field(default=None, description="Liveness timeout (s) for this node, bypassing the duration vocabulary.")
    model: str | None = Field(default=None, description="Model for this node (overrides the run-wide and flow-declared model).")
    agent_dir: str | None = Field(default=None, description="agent_dir for this node (overrides the run-wide and flow-declared agent_dir).")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description="Runtime-SPECIFIC flags for this node (e.g. {serve_url: ...}), merged over run-wide options. Open bag; the runtime reads it.",
    )


def runtime_param(**extra: Any) -> dict[str, Any]:
    """Marker for a runtime-populated params_model field, for `Field(json_schema_extra=...)`.

    A field carrying this is NOT a user input: it holds a placeholder at startup
    and is filled at run time (a node's exports, a default_factory, …). `run_cli`
    recognises the marker and OMITS the field from the resolved-params summary.

    Usage in a consumer's params model:

        from agent_flow import runtime_param
        pipeline_commit: str = Field(default="UNKNOWN", json_schema_extra=runtime_param())

    Extra keys are merged in, so you can attach your own schema metadata too.
    """
    return {RUNTIME_PARAM_KEY: True, **extra}


def runtime_param_fields(model: type | None) -> set[str]:
    """Names of a params_model's fields marked runtime-populated (see runtime_param).

    Returns an empty set when there is no model / no such fields.
    """
    fields = getattr(model, "model_fields", None)
    if not fields:
        return set()
    out: set[str] = set()
    for fname, finfo in fields.items():
        extra = getattr(finfo, "json_schema_extra", None)
        if isinstance(extra, dict) and extra.get(RUNTIME_PARAM_KEY):
            out.add(fname)
    return out


def _is_inline_json(value: str) -> bool:
    """True when a --config value is inline JSON (starts with `{` or `[`) vs a file path.

    A leading `[` is included so an inline array is parsed and then rejected by the
    mapping check — a clearer error than treating it as a (missing) file path.
    """
    stripped = value.strip()
    return stripped.startswith("{") or stripped.startswith("[")


def _load_config_source(value: str) -> dict[str, Any]:
    """Read one --config value — inline JSON `{...}` or a YAML/JSON file path — to a dict."""
    import json

    import yaml

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


def _validate_config_keys(data: dict[str, Any]) -> dict[str, Any]:
    """Reject unknown top-level keys (a typo), against RunConfig fields + `params`.

    Shared by the --config assembly and the programmatic run_config= base, so both
    fail loudly on the same typo. Returns the dict unchanged for chaining.
    """
    allowed = set(RunConfig.model_fields) | {"params"}
    unknown = set(data) - allowed
    if unknown:
        raise ValueError(f"unknown run config keys {sorted(unknown)} (allowed: {sorted(RunConfig.model_fields)} + params)")
    return data


def _assemble_config(sources: list[str]) -> dict[str, Any]:
    """Load each --config source in order and deep-merge them (later wins).

    Validates the top-level keys of the MERGED result once (so a typo in any
    layer fails loudly). Returns the merged dict, ready to feed the settings model.
    """
    merged: dict[str, Any] = {}
    for value in sources:
        merged = _deep_merge(merged, _load_config_source(value))
    return _validate_config_keys(merged)


class RunConfig(BaseSettings):
    """Resolved generic run configuration (the library's own CLI settings).

    Domain params are NOT here — they belong to a flow-supplied settings model.
    Env vars use the AGENT_FLOW_ prefix (e.g. AGENT_FLOW_RUN_DIR).

    Pass `--config` source(s) via the `_config_sources` init kwarg (a path or
    inline JSON, or a list of either deep-merged in order); the CLI does this for
    `--config`. The merged result is read below env/.env but above field defaults.
    """

    model_config = SettingsConfigDict(
        env_prefix="AGENT_FLOW_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    runtime: str = Field(
        default="opencode", description="Agent runtime: the real out-of-process runner (e.g. 'opencode'). Not a mock — see mock_agents."
    )
    mock_agents: bool = Field(
        default=False,
        description=(
            "Substitution MODE (not a runtime): run each node whose agent has a registered mock_agent "
            "via MockExecutor (deterministic, no tokens); nodes without one still run for real (partial mocking)."
        ),
    )
    backend: str = Field(
        default="inprocess",
        description="Execution backend: 'inprocess' (default; runs the DAG in this process, no Prefect) or 'prefect' (opt-in run UI/scale).",
    )
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
    nodes: dict[str, NodeRunConfig] = Field(
        default_factory=dict,
        description=(
            "Per-node run settings {node: {instructions, duration, idle_timeout_s, model, agent_dir}}. "
            "Each field overrides the run-wide value for that one node. Node keys are validated against the "
            "flow (an unknown name is a hard error). --instruct NODE=text populates nodes.<node>.instructions."
        ),
    )
    model: str = Field(
        default="",
        description="Model (provider/model) for every node. Empty -> the runtime resolves it from its own config. Per-node model overrides.",
    )
    idle_timeout_s: int = Field(
        default=DEFAULT_IDLE_TIMEOUT_S,
        description="Liveness timeout (s): kill an agent only after this long with no event/sidecar. Used by nodes that declare no `duration`.",
    )
    durations: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Duration vocabulary {name: seconds} for nodes that declare a portable `duration` "
            "(e.g. {long: 900}). Overlays the shipped short/normal/long, so retuning one name keeps the rest."
        ),
    )
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Runtime-SPECIFIC flags applied to every node (e.g. {serve_url: 'http://localhost:4096'}). "
            "An open bag the runtime interprets; a per-node `nodes.<n>.options` entry merges over it."
        ),
    )

    # The assembled --config data for the current construction. Stashed on the class
    # by __init__ so the settings_customise_sources CLASSMETHOD (which sees only
    # settings_cls, not the instance) can read it. Not thread-safe by construction,
    # which is fine: the CLI builds a RunConfig once per process. Holds the
    # already-assembled + validated config dict (merged across every --config).
    _af_config_data: dict[str, Any] | None = None
    # The programmatic `run_config=` base layer — the pipeline author's own
    # defaults, BELOW --config in precedence. Same class-stash mechanism as above.
    _af_base_data: dict[str, Any] | None = None

    def __init__(self, _config_sources: str | Path | list[str] | None = None, _base: dict[str, Any] | None = None, **kwargs: Any) -> None:
        # Assemble the --config layer up front: load each source (path or inline
        # JSON), deep-merge in order, validate the merged top-level keys. One
        # value or a list are both accepted; None means "no --config". `_base` is
        # the programmatic run_config= dict — validated the same way, applied below.
        sources: list[str] = []
        if _config_sources:
            sources = [str(_config_sources)] if isinstance(_config_sources, (str, Path)) else [str(s) for s in _config_sources]
        type(self)._af_config_data = _assemble_config(sources) if sources else None
        type(self)._af_base_data = _validate_config_keys(dict(_base)) if _base else None
        try:
            super().__init__(**kwargs)
        finally:
            type(self)._af_config_data = None
            type(self)._af_base_data = None

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        data = getattr(settings_cls, "_af_config_data", None)
        base = getattr(settings_cls, "_af_base_data", None)
        # Precedence (first wins): CLI/init > env > .env > --config > run_config=
        # base > defaults. Both config layers are fed via InitSettingsSource (plain
        # dict sources); the --config layer sits ABOVE the programmatic base.
        sources: tuple[PydanticBaseSettingsSource, ...] = (init_settings, env_settings, dotenv_settings)
        if data:
            sources = (*sources, InitSettingsSource(settings_cls, init_kwargs=data))
        if base:
            sources = (*sources, InitSettingsSource(settings_cls, init_kwargs=base))
        return sources

    def resolved_instructions(self) -> str:
        """This run's ADDITIONAL run-wide brief (the -i/--instructions value):
        instructions_file content if given, else the inline `instructions`.

        This is the run-config layer ONLY — prompt channel [4]. It is rendered
        AFTER the flow's standing `run_instructions` (channel [3]) as its own
        block; it does not replace it. See design/input-plane.md."""
        if self.instructions_file:
            return Path(self.instructions_file).read_text()
        return self.instructions

    def set_node_instruction(self, node: str, text: str) -> None:
        """Set nodes.<node>.instructions, creating the entry if absent (CLI --instruct)."""
        entry = self.nodes.get(node) or NodeRunConfig()
        self.nodes[node] = entry.model_copy(update={"instructions": text})

    def node_overrides(self) -> dict[str, dict[str, Any]]:
        """The `nodes:` section projected to the plain dicts the ENGINE consumes.

        The engine takes `dict[str, dict]`, never the NodeRunConfig type, so it
        stays free of the settings module (tier discipline). One projection here
        rather than at each entry point — the CLI and the programmatic path must
        not hand-copy it and drift.
        """
        return {name: nc.model_dump() for name, nc in self.nodes.items()}

    def apply_run_wide_params(self, params: dict[str, Any]) -> dict[str, Any]:
        """Inject the run-wide knobs a node reads back out of `params`, in place.

        `model` / `idle_timeout_s` reach a node via `ctx.params` (see the reserved
        param names in input-plane.md), so the resolved config values must be
        seeded there. `setdefault` so an explicit `-p` (or a per-node value) still
        wins. `model` only when set — empty means "let the runtime resolve it".

        Shared by BOTH entry points on purpose: the programmatic path once missed
        this and `run_config={"model": ...}` was silently dropped while the CLI
        worked. One method, no drift.
        """
        if self.model:
            params.setdefault("model", self.model)
        params.setdefault("idle_timeout_s", str(self.idle_timeout_s))
        return params


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
