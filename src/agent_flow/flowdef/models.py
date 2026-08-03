"""FlowDef / NodeDef — the serializable pipeline definition (pydantic).

Pure DATA: every field is JSON/YAML-serializable. Code (the agent run, gates,
exports, custom runs, result schemas) is referenced BY NAME and resolved against
a FlowRegistry at compile time — never embedded here. This is the official
authoring surface; it compiles to the internal runtime `Node` (see compile.py).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class NodeDef(BaseModel):
    """One node of the pipeline, as data. Runs an AGENT (`agent`) or a registered
    custom run (`run_ref`) — exactly one of the two.

    All fields mirror agent_node's authoring options, but as serializable data:
    gates/exports/schemas are NAMES resolved via the registry, not callables.
    """

    model_config = {"extra": "forbid"}

    name: str
    # Exactly one run-source (validated below):
    agent: str | None = None  # standard 'run one agent' (opencode --agent name)
    run_ref: str | None = None  # a registered custom run impl (registry.run)

    # Work-order + prompt inputs (templated with {param} at run time).
    inputs: dict[str, str] = Field(default_factory=dict)
    instructions: str = ""
    context: list[str] = Field(default_factory=list)  # file sources injected as content

    # DAG wiring + flow-control knobs.
    depends_on: list[str] = Field(default_factory=list)
    parallel_group: str | None = None
    criticality: Literal["blocking", "degrade"] = "blocking"
    max_cycles: int = 1

    # Flow-control gate (by name) + its data args. Resolved via registry.
    gate: str | None = None
    gate_args: dict[str, Any] = Field(default_factory=dict)

    # The re-run GRANT: node/parallel-group names this node's agent may ask to
    # run again. Declaring it is the opt-in — it names the legal targets in the
    # agent's preamble and authorizes the jump; no gate is involved. Validated at
    # build time (known name, and backward of this node). See flow_types.Node.
    rerun_targets: list[str] = Field(default_factory=list)

    # Result-schema by registered name (validated + injected by the agent run).
    result_schema: str | None = None

    # Input-schema by registered name — the mirror of result_schema, applied to
    # the RESOLVED work order (after templating/exports). Same registry, so the
    # FlowDef stays pure serializable data: the NAME travels, the model class
    # lives in code.
    input_schema: str | None = None

    # Result -> params publishing for downstream nodes: a declarative
    # {param: field} map, OR a registered export impl by name (export_ref).
    exports: dict[str, str] | None = None
    export_ref: str | None = None

    # In-process execution: a registered agent-impl name (registry.agent_impl).
    # When set, the node runs the agent as a direct in-process call (no
    # subprocess/sidecar) via InProcessExecutor. If `agent` is omitted it
    # defaults to `name` (the common case where name == agent == impl_ref).
    impl_ref: str | None = None

    # How long this node is EXPECTED to take, as a portable name from the
    # duration vocabulary ("short"/"normal"/"long", or any name the run config
    # defines). The flow declares the INTENT; the run config maps the name to
    # concrete seconds (`durations:`). A raw second-count does not belong here —
    # it is an environment fact, not a property of the pipeline.
    #
    # `model` and `agent_dir` deliberately do NOT live here: a provider/model
    # string and a filesystem path are environment facts, not properties of the
    # portable pipeline. Set them per node via the run config's `nodes:` section
    # (RunConfig.nodes.<name>.model / .agent_dir), or run-wide via the top-level
    # model / agent_dir. A hand-written (programmatic) flow may still pass them to
    # agent_node() directly — that path is code, not serialized data.
    duration: str | None = None

    @model_validator(mode="after")
    def _one_run_source(self) -> NodeDef:
        # When impl_ref is set and agent is omitted, default agent to name.
        # This is the common case (name == agent == impl_ref); the explicit agent=
        # form is still accepted when you want a different label or mock_agent key.
        if self.impl_ref is not None and not self.agent:
            self.agent = self.name
        if bool(self.agent) == bool(self.run_ref):
            raise ValueError(f"node {self.name!r}: set exactly one of `agent` or `run_ref`")
        if self.exports is not None and self.export_ref is not None:
            raise ValueError(f"node {self.name!r}: set at most one of `exports` or `export_ref`")
        return self


class FlowDef(BaseModel):
    """A whole pipeline as data: nodes + flow-wide settings. Serializable."""

    model_config = {"extra": "forbid"}

    name: str = "agent-flow"
    nodes: list[NodeDef] = Field(min_length=1)

    # Flow-wide PORTABLE declarations. Named for their SCOPE: `run_*` spans the
    # whole run; a NodeDef's `instructions`/`context` are one node. Same word,
    # different scope — see docs/design/input-plane.md.
    #
    # agent_dir / backend / llm_concurrency are NOT here: a filesystem path, a
    # deployment choice, and an environment capacity are run config, not portable
    # pipeline data. Supply them via run_config= / --config / the CLI / env.
    run_instructions: str = ""
    run_context: list[str] = Field(default_factory=list)

    # The flow's SIGNATURE: a registered params model BY NAME (registry
    # .params_model) declaring the run parameters this pipeline needs —
    # product_key, … — the values its nodes template as `{name}`. The mirror of
    # NodeDef.input_schema, one scope up: a node declares its input contract, a
    # flow declares its own. Kept as a NAME so the FlowDef stays serializable
    # (the model class lives in code, like gates/schemas). Unset -> params pass
    # through untyped, exactly as before.
    params_schema: str | None = None

    @model_validator(mode="after")
    def _validate_graph(self) -> FlowDef:
        names = [n.name for n in self.nodes]
        known = set(names)
        if len(names) != len(known):
            dupes = sorted({n for n in names if names.count(n) > 1})
            raise ValueError(f"duplicate node names: {dupes}")
        for n in self.nodes:
            for dep in n.depends_on:
                if dep not in known:
                    raise ValueError(f"node {n.name!r} depends_on unknown node {dep!r}")
        return self

    def to_json(self, *, indent: int | None = 2) -> str:
        """Canonical JSON (defaults/nulls omitted) \u2014 the storable/transferable form."""
        return self.model_dump_json(exclude_defaults=True, exclude_none=True, indent=indent)
