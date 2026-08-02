"""`RunConfig` and `NodeRunConfig` — the run-scoped settings model.

The flow declares what the pipeline IS, SAYS and NEEDS; this declares HOW and
WHERE this particular run behaves. Every field is an environment fact — a model
string, a path, a timeout — never a property of the portable pipeline.

Precedence, most specific first: CLI flag > AGENT_FLOW_* env > .env file >
--config source > programmatic base > field default.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict
from pydantic_settings.sources import InitSettingsSource

from agent_flow.const import DEFAULT_IDLE_TIMEOUT_S, DEFAULT_MAX_RETRIES
from agent_flow.run_config.sources import _assemble_config, _validate_config_keys

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
    max_retries: int | None = Field(
        default=None,
        description="Retries for this node after a TRANSIENT agent failure (stale/hung or crashed); overrides the run-wide max_retries.",
    )
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
    max_retries: int = Field(
        default=DEFAULT_MAX_RETRIES,
        description=(
            "Retries after a TRANSIENT agent failure — the agent hung (stale-killed) or its process crashed. "
            "Applied PER NODE and isolated: a retried node's parallel siblings keep their outcomes. "
            "A failure the agent DIAGNOSED itself is never retried. When retries are exhausted the node's "
            "`criticality` decides: degrade -> continue, blocking -> stop the run. 0 disables retrying."
        ),
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
        _allowed = set(RunConfig.model_fields) | {"params"}
        type(self)._af_config_data = _assemble_config(sources, _allowed) if sources else None
        type(self)._af_base_data = _validate_config_keys(dict(_base), _allowed) if _base else None
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
