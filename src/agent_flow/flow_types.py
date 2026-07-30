"""Flow DAG TYPES — the vocabulary the engine, the backends and a node's
`run` callable all speak.

A pure LEAF module: it imports only `gates` (for the `Gate` type) and the
standard library, and it imports nothing from the engine, the backends, the
runners or `core`. That is the whole point of the module.

The vocabulary sits below both the engine and the backends because both need it:
the backend ABC and every backend take `Node`/`NodeOutcome` in their signatures,
and `engine.build_flow` resolves a backend by name. Parameterising the execution
seam with a type owned by the thing being sequenced is the wrong direction for a
swappable backend — a backend depends on the vocabulary, not on the engine it is
swappable for. So the arrows point one way:

    backends -> flow_types <- engine
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from agent_flow.gates import Gate

Criticality = Literal["blocking", "degrade"]

# Default bound on re-run cycles (Restart / GoTo-to-self) per node per run.
DEFAULT_MAX_CYCLES = 1


class NodeBlocked(RuntimeError):
    """A blocking-criticality node failed or its gate returned Stop."""


@dataclass(frozen=True)
class RunContext:
    """What a node's `run` callable receives.

    node     the Node being executed.
    run_dir  the run's directory (already created): where control sidecars go and
             the base for relative artifact paths. NOT a cwd; NOT where agent
             definitions live.
    cycles   how many times this node has been re-run so far (0 on first run).
    params   pipeline-supplied DOMAIN run parameters (product key, repos root,
             …) threaded through build_flow unchanged. The engine does not
             interpret these — the run callable does. Domain-opaque by design.
    on_event_factory  optional per-node event-printer FACTORY: given a display
             LABEL (the agent-node passes the NODE name — the DAG unit the
             reader navigates by, not the agent that implements it), returns a
             per-event callback (or None) — the ACTUAL callback
             `run_agent(on_event=...)` expects at Tier 1. Named differently from
             that Tier-1 `on_event` on purpose: at Tier 3 the label is not known
             until inside a node's `run`, so this is a factory, not a callback.
             It is ENGINE plumbing, not a domain param — a build-time concern
             (set via build_flow), kept out of `params` and off the
             task-serialization path since a callable is not serializable.
    """

    node: Node
    run_dir: Path
    cycles: int
    params: dict[str, Any]
    on_event_factory: Callable[[str], Any] | None = None
    # Default directory where agent DEFINITIONS live (opencode --dir), from
    # build_flow; a node may override via agent_node(agent_dir=...). Templated.
    agent_dir: str = ""
    # Run-wide STANDING brief injected into EVERY agent, declared on the flow.
    # Engine plumbing, not a domain param.
    run_instructions: str = ""
    # This run's ADDITIONAL run-wide brief (the -i / config `instructions` value),
    # rendered AFTER run_instructions — additive, never replacing it. Its own
    # channel so the two read as distinct blocks (mirrors the per-node pair).
    run_additional_instructions: str = ""
    # Run-wide context SOURCES (file paths / globs) whose CONTENT is injected
    # into every agent — rules/standards the agent must actually have. Read at
    # run time (per node, so templating against params works). Build-time
    # plumbing, not a domain param.
    run_context: tuple[str, ...] = ()
    # Run-time per-node overrides {node_name: {instructions, model, agent_dir,
    # duration, idle_timeout_s}}, from the run config's `nodes:` section (CLI
    # --instruct populates .instructions). Each entry overrides the flow-declared
    # value for that one node — this is the "how THIS run behaves" layer, more
    # specific than the flow's standing declaration. Plain dicts (not the settings
    # NodeRunConfig type) so the engine stays free of the settings module.
    node_overrides: dict[str, dict[str, Any]] = field(default_factory=dict)
    # The run's duration VOCABULARY {name: seconds}, from build_flow (RunConfig
    # `durations:`). A node declares a portable NAME (Node.duration); this map
    # supplies the environment's concrete seconds. Overlays the shipped defaults.
    durations: dict[str, int] = field(default_factory=dict)
    # Run-wide runtime-SPECIFIC options {key: value}, from build_flow (RunConfig
    # `options:`). An open bag the RUNTIME interprets (e.g. serve_url); the engine
    # never looks inside. A node's own `node_overrides[name]["options"]` merges
    # OVER this at the executor seam.
    options: dict[str, Any] = field(default_factory=dict)
    # A ONE-TIME instruction for THIS run attempt only. Today it is set by the
    # engine from a gate's Restart/GoTo `instruction`, but the field's nature is
    # general: a single-attempt instruction handed to a node's next run, not
    # intrinsically about re-running. Unlike the per-node instruction channels
    # (standing, whole run) it is ephemeral: the node's `run` appends it LAST
    # (freshest guidance, right before the work order), and the engine clears it so
    # the next attempt does not inherit it. It is injected VERBATIM — the engine
    # imposes NO heading or wrapping; the caller that produced it owns the full
    # framing. Plain-text prompt content, NOT a param. Empty on a first/clean run.
    one_time_instruction: str = ""
    # The run's FlowRegistry — the consumer's namespace of gates, exports,
    # schemas, mock agents and renderer overrides. Run-scoped, not node-scoped:
    # one registry serves every node in a flow. It reaches a node from here
    # rather than being restated per node, so a node cannot end up consulting a
    # different registry than the flow was built with — which silently disabled
    # --mock-agents for any node that omitted it. Typed loosely to keep this
    # module a leaf (registry sits above the flow vocabulary).
    registry: Any | None = None


# A node's work: perform the invocation, return whatever the gate will inspect
# (typically the control dict or an AgentResult). May raise on hard failure.
# Additive since the async-first migration: the callable may be sync (`def`) OR async (`async def`) —
# the engine awaits an awaitable return via _maybe_await. agent_node's closure is
# async; hand-written `run` nodes may be either.
RunFn = Callable[[RunContext], Awaitable[Any] | Any]


@dataclass(frozen=True)
class Node:
    """One node of the pipeline DAG.

    name           unique node id.
    run            callable that performs the work (see RunFn / RunContext).
    gate           optional flow-control gate; absent means always Continue.
    depends_on     upstream node names that must finish first (the DAG edges).
    parallel_group nodes sharing a group name fan out concurrently.
    criticality    'blocking' -> failure/Stop raises NodeBlocked (halts run);
                   'degrade'  -> failure is recorded as 'degraded', run continues.
    max_cycles     per-node bound on Restart / self-GoTo re-runs.
    result_schema  optional ResultSchema | JSON-schema dict | pydantic BaseModel
                   subclass for the agent's `result` payload. The run callable
                   passes it to run_agent, which injects it into the prompt and
                   validates the output.
    input_schema   the MIRROR of result_schema, for the node's INPUTS: the same
                   accepted forms, validated against the RESOLVED work order
                   (after `{param}` templating and upstream `exports`). Values
                   still live in `inputs` — a schema is their TYPE, so several
                   nodes may share one schema with different values. Invalid
                   input fails the node BEFORE its agent runs, mapped through
                   `criticality` like any other node error. An in-process impl
                   receives the validated instance as `inv.input_obj`.
    duration       optional DECLARED duration name ("short"/"normal"/"long", or
                   any name the run's `durations` map defines). Portable intent;
                   build_flow resolves it to seconds and REJECTS an unknown name
                   at build time, so a typo cannot survive to run time.
    agent          optional INFORMAL display label: the agent this node runs.
                   Purely cosmetic — the engine never uses it for logic (a node's
                   work is its `run` callable). Set automatically by agent_node;
                   surfaced via on_node_event and shown in progress/result output.
                   Blank for hand-written nodes.
    """

    name: str
    run: RunFn
    gate: Gate | None = None
    depends_on: tuple[str, ...] = ()
    parallel_group: str | None = None
    criticality: Criticality = "blocking"
    max_cycles: int = DEFAULT_MAX_CYCLES
    result_schema: object = None
    input_schema: object = None
    duration: str = ""
    agent: str = ""
    # Optional result->params export hook. After the node completes (and is not
    # re-running), the engine derives keys from the node's result and merges them
    # into the run-context service so DOWNSTREAM nodes template against them.
    # Either a callable `(result) -> Mapping[str, Any]`, or a declarative map
    # `{param_name: result_field}`. See agent_node(exports=...) and run_context.
    exports: Callable[[dict[str, Any]], Any] | dict[str, str] | None = None
    # Data references into a FlowRegistry (the serializable form; resolved by the
    # engine at run time). A node's flow-control is a NAME + args, not a baked-in
    # callable — so a node/definition stays serializable and the impl lives once
    # in the registry. gate_ref names a registered gate factory; gate_args are
    # its kwargs. export_ref names a registered export impl (the inline `exports`
    # dict/callable above still works and takes precedence when set). When gate_ref
    # is set it is resolved via the registry; when only `gate` (a callable) is set
    # the engine registers it under a generated ref. See registry.FlowRegistry.
    gate_ref: str | None = None
    gate_args: dict[str, Any] = field(default_factory=dict)
    export_ref: str | None = None


@dataclass(frozen=True)
class NodeOutcome:
    """Result of running one node to completion.

    status       'ok' | 'degraded'.
    goto         a node name to resume the flow at, when the gate returned a
                 cross-node GoTo (jump-back). None for the common case. The walker
                 (build_flow) honors it; interpret handles only the self-loop.
    instruction  the one-time instruction attached to a cross-node GoTo, for the
                 walker to deliver to the TARGET node's next run. Empty otherwise.
    duration_s   wall-clock seconds the node took (set by build_flow's task; 0.0
                 when produced by interpret directly, e.g. in unit tests).
    runtime      the canonical runtime label of HOW the node's agent ran
                 ("opencode" / "claude" / "inproc" / "mock"). Empty for a
                 hand-written `run` node (no agent) or when the node errored
                 before producing a result.
    """

    status: str
    goto: str | None = None
    instruction: str = ""
    duration_s: float = 0.0
    runtime: str = ""
