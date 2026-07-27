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

    # Per-node runtime overrides.
    model: str | None = None
    idle_timeout_s: int | None = None
    agent_dir: str | None = None

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

    # Flow-wide (mirror build_flow's run-wide knobs).
    shared_instructions: str = ""
    shared_context: list[str] = Field(default_factory=list)
    agent_dir: str = ""
    backend: str = "inprocess"
    llm_concurrency: int | None = None

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
