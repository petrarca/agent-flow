"""Declaration-driven engine — compiles a Node graph into a runnable flow.

This is the library's Layer-3 API: declare a pipeline as a list of `Node`s and
`build_flow` returns a runnable flow callable that walks the DAG, fans out
parallel groups, invokes each node's gate, and interprets the returned directive
(Continue / Restart / GoTo / Stop) with bounded re-run cycles and per-node
criticality. Execution is dispatched to a `FlowBackend` (the in-process backend
by default, Prefect opt-in); the engine itself owns the flow logic and is
backend-free.

The engine is deliberately AGNOSTIC to what a node does. It knows nothing about
"analysts", "verifiers", reports, or prompts — a `Node` carries a `run` callable
that performs the actual work (build a prompt, call `run_agent`, run a composite
of several agents, whatever). The engine only orchestrates: order, parallelism,
gate directives, criticality. Domain knowledge lives in `run` and `gate`.

Two layers of use:
  - Layer 3 (this module): declare Nodes, call `build_flow`.
  - Layer 2 (no this module): call `run_agent` directly as the leaf of your own
    flow. `build_flow` is optional, not required.

The DAG walk and gate interpretation are pure helpers (`plan_groups`,
`interpret`, `_walk`), so the orchestration logic is unit-testable in-process
with no execution backend.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from inspect import isawaitable
from pathlib import Path
from typing import Any, Literal

from agent_flow.gates import Continue, Gate, GateContext, GoTo, Restart, Stop
from agent_flow.utils import resolve_duration


async def _maybe_await(value: Any) -> Any:
    """Await `value` if it is awaitable, else return it as-is.

    The single dispatch point that makes every consumer callable additive: a
    node's `run`, a gate, an export, and observing hooks may each be sync OR
    async. Sync callables return a plain value (passed through); async ones
    return a coroutine (awaited here). Miss a call site and async consumers
    silently break — so ALL of them route through this.
    """
    if isawaitable(value):
        return await value
    return value


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
    # Run-wide instruction/brief injected into EVERY agent (build-time, from the
    # orchestrator start / CLI). Engine plumbing, not a domain param.
    run_instructions: str = ""
    # Run-wide context SOURCES (file paths / globs) whose CONTENT is injected
    # into every agent — rules/standards the agent must actually have. Read at
    # run time (per node, so templating against params works). Build-time
    # plumbing, not a domain param.
    run_context: tuple[str, ...] = ()
    # Run-time per-node instructions {node_name: text}, from CLI --instruct / the
    # config node_instructions: section. A agent-node appends its own entry
    # LAST (after the build-time per-node instructions), so it is the most recent
    # standing guidance before the work order — additive, last-word override.
    node_instructions: dict[str, str] = field(default_factory=dict)
    # The run's duration VOCABULARY {name: seconds}, from build_flow (RunConfig
    # `durations:`). A node declares a portable NAME (Node.duration); this map
    # supplies the environment's concrete seconds. Overlays the shipped defaults.
    durations: dict[str, int] = field(default_factory=dict)
    # A ONE-TIME instruction for THIS run attempt only. Today it is set by the
    # engine from a gate's Restart/GoTo `instruction`, but the field's nature is
    # general: a single-attempt instruction handed to a node's next run, not
    # intrinsically about re-running. Unlike node_instructions (standing, whole
    # run) it is ephemeral: the node's `run` appends it as the LAST prompt block
    # (freshest guidance, right before the work order), and the engine clears it so
    # the next attempt does not inherit it. It is injected VERBATIM — the engine
    # imposes NO heading or wrapping; the caller that produced it owns the full
    # framing. Plain-text prompt content, NOT a param. Empty on a first/clean run.
    one_time_instruction: str = ""


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


def _group_membership(nodes: list[Node]) -> tuple[dict[str, list[Node]], list[str]]:
    """Bucket nodes by parallel_group (or their own name), preserving order."""
    groups: dict[str, list[Node]] = {}
    order: list[str] = []
    for n in nodes:
        key = n.parallel_group or n.name
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(n)
    return groups, order


def _group_dependencies(groups: dict[str, list[Node]], order: list[str], by_name: dict[str, Node]) -> dict[str, set[str]]:
    """Map each group to the groups it depends on. Raises on unknown deps."""
    group_of = {name: (n.parallel_group or n.name) for name, n in by_name.items()}
    group_deps: dict[str, set[str]] = {}
    for key in order:
        deps: set[str] = set()
        for n in groups[key]:
            for d in n.depends_on:
                if d not in by_name:
                    raise ValueError(f"node {n.name!r} depends on unknown node {d!r}")
                if group_of[d] != key:
                    deps.add(group_of[d])
        group_deps[key] = deps
    return group_deps


def _toposort(order: list[str], group_deps: dict[str, set[str]]) -> list[str]:
    """Kahn topological sort over groups, tie-broken by declaration order."""
    done: set[str] = set()
    planned: list[str] = []
    remaining = list(order)
    while remaining:
        ready = [k for k in remaining if group_deps[k] <= done]
        if not ready:
            raise ValueError(f"dependency cycle among groups: {remaining}")
        for k in ready:
            planned.append(k)
            done.add(k)
            remaining.remove(k)
    return planned


def _check_unique_names(nodes: list[Node]) -> None:
    """Raise unless every node name is unique — `Node.name` IS the node's id.

    Names key the whole engine: `by_name`, results, `depends_on`, `--only` /
    `--start-from`, and GoTo targets. A duplicate silently collapses those (the
    last definition wins, so an earlier node's `run` never executes while the
    later one runs once per duplicate) — a wrong result with no error. Caught
    here at build time, alongside cycles and unknown deps.
    """
    seen: set[str] = set()
    dupes: list[str] = []
    for n in nodes:
        if n.name in seen and n.name not in dupes:
            dupes.append(n.name)
        seen.add(n.name)
    if dupes:
        raise ValueError(f"duplicate node name(s): {sorted(dupes)} — every Node.name must be unique (it is the node's id)")


def plan_groups(nodes: Iterable[Node]) -> list[tuple[str, list[Node]]]:
    """Order nodes into execution groups honoring depends_on and parallel_group.

    Returns a list of (group_key, nodes) in a valid execution order: every
    group's dependencies appear in an earlier group. Nodes sharing a
    parallel_group run together; solo nodes are their own single-member group.

    Raises ValueError on a DUPLICATE node name, an unknown dependency, or a
    dependency cycle — all caught at BUILD time, before anything runs.
    """
    nodes = list(nodes)
    _check_unique_names(nodes)
    by_name = {n.name: n for n in nodes}
    groups, order = _group_membership(nodes)
    group_deps = _group_dependencies(groups, order, by_name)
    return [(key, groups[key]) for key in _toposort(order, group_deps)]


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


def _make_node_emitter(on_node_event: Callable[[str, str, str | None, str], None] | None) -> Callable[[str, str, str | None, str], None]:
    """Return a node-lifecycle emitter: the user callback, or a no-op if None.

    Keeps the None-guard out of build_flow's body (one bound callable there),
    so build_flow stays under the complexity limit and the emit sites read as
    unconditional calls.
    """
    if on_node_event is None:
        return lambda _name, _phase, _status, _agent: None
    return on_node_event


async def interpret(
    node: Node,
    *,
    run_dir: Path,
    params: dict[str, Any],
    on_error: Callable[[Node, Exception], str],
    log: Callable[[str], None] = lambda _msg: None,
    on_event_factory: Callable[[str], Any] | None = None,
    run_instructions: str = "",
    run_context: tuple[str, ...] = (),
    agent_dir: str = "",
    node_instructions: dict[str, str] | None = None,
    durations: dict[str, int] | None = None,
    registry: Any = None,
    one_time_instruction: str = "",
) -> NodeOutcome:
    """Run one node to completion, interpreting its gate's directives.

    Returns a NodeOutcome. Raises NodeBlocked when a blocking node fails or its
    gate returns Stop. Restart (and GoTo-to-self) re-run the node in place,
    bounded by max_cycles. A cross-node GoTo(other) is NOT handled here — it is
    surfaced via NodeOutcome.goto for the walker to act on (jump-back).

    `one_time_instruction` is a single-attempt instruction for the FIRST attempt
    only (the walker passes it when the flow resumes at this node from a
    cross-node GoTo, so the target receives the gate's `instruction` — note a
    GoTo is a RESUME, not necessarily a re-run). It is folded into that attempt's
    prompt and then cleared; a self-loop Restart/self-GoTo sets a fresh one from
    its own directive for the next in-place attempt.

    `on_error` maps a raised exception to a status per criticality (and may
    itself raise NodeBlocked). `registry` (a FlowRegistry) resolves the node's
    gate (from `gate_ref`+`gate_args`, or a callable `gate` auto-registered) and
    export_ref, and receives observing hooks; a default is created if None.
    """
    from agent_flow.run_context import get_run_context

    if registry is None:
        from agent_flow.registry import FlowRegistry

        registry = FlowRegistry()
    gate = _resolve_gate(node, registry)

    cycles = 0
    # One-time-instruction carrier: seeded from the walker (cross-node GoTo
    # delivery), then re-set from each self Restart/GoTo directive. Consumed into
    # an attempt's prompt and cleared immediately after, so it applies to exactly
    # one attempt (see RunContext.one_time_instruction).
    pending_instruction = one_time_instruction
    while True:
        # Observing hook: before each run attempt (fires again on re-run cycles).
        await _fire_hook(registry, "before_node", node, log, node)
        # Effective params = the passed params overlaid with the live run-context
        # snapshot, so this node sees any values UPSTREAM nodes exported. Snapshot
        # is taken at THIS node's start -> a stable view for its whole execution.
        eff_params = {**params, **get_run_context().snapshot()}
        # Hand this attempt its one-time instruction (if any), then clear the
        # carrier: it is now the node's to render; a subsequent cycle starts blank
        # unless the gate below sets a fresh one.
        attempt_instruction = pending_instruction
        pending_instruction = ""
        try:
            result = await _maybe_await(
                node.run(
                    RunContext(
                        node=node,
                        run_dir=run_dir,
                        cycles=cycles,
                        params=eff_params,
                        on_event_factory=on_event_factory,
                        run_instructions=run_instructions,
                        run_context=run_context,
                        agent_dir=agent_dir,
                        node_instructions=dict(node_instructions or {}),
                        durations=dict(durations or {}),
                        one_time_instruction=attempt_instruction,
                    )
                )
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad: the run callable is caller code
            await _fire_hook(registry, "on_error", node, log, node, exc)
            return NodeOutcome(status=on_error(node, exc))

        # The validated typed object (a pydantic instance when a result_schema was
        # set, else None) — surfaced as GateContext.obj so gates/exports read it
        # directly instead of digging the `_result_obj` key out of `result`.
        obj = result.get("_result_obj") if isinstance(result, dict) else None
        # The runtime label the executor stamped (agent nodes only; empty for a
        # hand-written run node). Carried onto every settled NodeOutcome below.
        runtime = result.get("_runtime", "") if isinstance(result, dict) else ""
        if gate:
            gctx = GateContext(result=result, obj=obj, node=node, run_dir=run_dir, cycles=cycles, params=eff_params, agent_dir=agent_dir)
            directive = await _maybe_await(gate(gctx))
        else:
            directive = Continue()

        if isinstance(directive, Stop):
            log(f"node {node.name}: gate -> Stop ({directive.reason})")
            raise NodeBlocked(directive.reason or node.name)

        restart = isinstance(directive, Restart) or (isinstance(directive, GoTo) and directive.node == node.name)
        if restart and cycles < node.max_cycles:
            cycles += 1
            # Carry the directive's one-time instruction into the NEXT attempt.
            pending_instruction = getattr(directive, "instruction", "")
            log(f"node {node.name}: gate -> re-run cycle {cycles} ({pending_instruction})")
            continue

        # Node is settling (Continue / cross-node GoTo / exhausted Restart): apply
        # its result->params exports to the run-context for DOWNSTREAM nodes.
        await _apply_exports(node, result, obj, log, registry)

        if isinstance(directive, GoTo) and directive.node != node.name:
            # Cross-node jump-back: the node itself is done; the walker decides
            # whether to rewind to the target (bounded there). Carry the one-time
            # instruction on the outcome so the walker can deliver it to the TARGET
            # node's next run.
            log(f"node {node.name}: gate -> GoTo {directive.node} ({directive.instruction})")
            outcome = NodeOutcome(status="ok", goto=directive.node, instruction=directive.instruction, runtime=runtime)
            await _fire_hook(registry, "after_node", node, log, node, outcome)
            return outcome

        # Continue, or an exhausted Restart.
        outcome = NodeOutcome(status="ok", runtime=runtime)
        await _fire_hook(registry, "after_node", node, log, node, outcome)
        return outcome


_MISSING = object()


def _read_field(src: Any, field: str) -> Any:
    """Read `field` from a dict (by key) or an object (by attribute); _MISSING if absent."""
    if isinstance(src, dict):
        return src.get(field, _MISSING)
    return getattr(src, field, _MISSING)


def _resolve_gate(node: Node, registry: Any):
    """Resolve a node's gate from the registry: gate_ref+args, or a callable gate.

    Precedence: an explicit gate_ref (data) wins; else a callable `node.gate`
    (back-compat) is auto-registered and used; else None (always Continue).
    """
    if node.gate_ref:
        return registry.build_gate(node.gate_ref, node.gate_args)
    if node.gate is not None:
        return node.gate  # a hand-supplied callable; used directly
    return None


async def _fire_hook(registry: Any, event: str, node: Node, log: Callable[[str], None], *fire_args: Any) -> None:
    """Fire an observing per-node hook (scoped to node.name). Never steers flow.

    An observer must never break the run — a failing hook is logged and ignored.
    `fire_args` are the event's payload (e.g. (node,) / (node, outcome) /
    (node, exc)); node.name is passed as the scope key so node-scoped hooks match.
    A hook may be sync or async: registry.fire returns each handler's result, and
    every awaitable among them is awaited here (async observers welcome).
    """
    try:
        for result in registry.fire(event, *fire_args, _node_name=node.name):
            await _maybe_await(result)
    except Exception as exc:  # noqa: BLE001 - an observer must never break the run
        log(f"node {node.name}: {event} hook failed ({exc}) — ignored")


async def _fire_group_hook(registry: Any, event: str, group: list[Node], warn: Callable[[str], None], *extra: Any) -> None:
    """Fire an observing group hook (before_group/after_group). Not node-scoped.

    Like _fire_hook, each handler's result is awaited when awaitable so an
    `async def` group hook actually runs."""
    try:
        for result in registry.fire(event, group, *extra):
            await _maybe_await(result)
    except Exception as exc:  # noqa: BLE001 - an observer must never break the run
        warn(f"{event} hook failed ({exc}) — ignored")


def _make_group_runner(backend_impl: Any, registry: Any, run_node: Any, logger: Any):
    """Wrap the backend's group execution with before_group/after_group hooks."""

    async def run_group(group: list[Node]) -> dict[str, NodeOutcome]:
        await _fire_group_hook(registry, "before_group", group, logger.warning)
        outcomes = await backend_impl.run_group(group, run_node)
        await _fire_group_hook(registry, "after_group", group, logger.warning, outcomes)
        return outcomes

    return run_group


async def _apply_exports(node: Node, result: Any, obj: Any, log: Callable[[str], None], registry: Any) -> None:
    """Merge a node's result-derived exports into the run-context service.

    The consumer's exports see the VALIDATED typed object when the node declared
    a `result_schema` (a pydantic instance), else the raw result dict — one
    payload, no signature sniffing. Sources (first present wins):
      - `node.export_ref` — a named export impl resolved via the registry.
      - `node.exports` callable `(payload) -> Mapping` — full control.
      - `node.exports` declarative `{param_name: field}` — copy fields.
    Missing fields are skipped. Never raises: a mistake logs and is ignored.
    """
    spec = node.exports
    if not spec and not node.export_ref:
        return
    from agent_flow.run_context import get_run_context

    payload = obj if obj is not None else result
    try:
        if node.export_ref:
            derived = dict(await _maybe_await(registry.get_export(node.export_ref)(payload)) or {})
        elif callable(spec):
            derived = dict(await _maybe_await(spec(payload)) or {})
        elif isinstance(spec, dict):  # the declarative {param_name: result_field} map
            derived = {param: v for param, fld in spec.items() if (v := _read_field(payload, fld)) is not _MISSING}
        else:  # unreachable: the guard above returns when neither spec nor export_ref is set
            derived = {}
        if derived:
            get_run_context().update(derived)
            log(f"node {node.name}: exported {sorted(derived)} to run-context")
    except Exception as exc:  # noqa: BLE001 - exports are consumer code; never fail the run
        log(f"node {node.name}: exports failed ({exc}) — ignored")


def _check_durations(nodes: list[Node], durations: dict[str, int]) -> None:
    """Reject an unknown duration name at BUILD time, with the whole graph in hand.

    Same instinct as plan_groups rejecting cycles here: a typo'd duration must not
    survive until that node's turn comes, halfway through a paid run.
    """
    for node in nodes:
        if node.duration:
            resolve_duration(node.name, node.duration, durations)


def build_flow(
    nodes: list[Node],
    *,
    name: str = "agent-flow",
    llm_tag: str = "llm",
    llm_concurrency: int | None = None,
    on_event_factory: Callable[[str], Any] | None = None,
    on_node_event: Callable[[str, str, str | None, str], None] | None = None,
    run_instructions: str = "",
    run_context: Iterable[str] | None = None,
    agent_dir: str = "",
    node_instructions: dict[str, str] | None = None,
    durations: dict[str, int] | None = None,
    backend: str = "inprocess",
    registry: Any = None,
):
    """Compile a Node graph into a runnable flow callable.

    Returns a callable `f(run_dir, start_from="", only="", **params) -> dict`
    that walks the DAG (`plan_groups`), fans out parallel groups concurrently,
    and runs each node via `interpret` (gate directives + criticality + bounded
    re-runs). `params` are threaded unchanged into every node's RunContext.

    The ENGINE owns all flow logic (plan/walk/jump-back/start_from/only/
    run-context); the selected `backend` supplies only execution mechanics
    (parallel fan-out, concurrency limit, logger, bootstrap/teardown). Backends
    are resolved lazily HERE so the engine module imports without pulling any
    backend (Prefect stays optional).

    Args:
        nodes: the pipeline DAG.
        name: flow name (used by the Prefect backend's @flow).
        llm_tag: concurrency tag applied to each node task (for a shared limit).
        llm_concurrency: if set, a concurrency limit on `llm_tag` (global on the
            Prefect backend; a per-process semaphore on the in-process backend).
        backend: execution backend name — "inprocess" (default; runs the DAG in
            this process via a threadpool, no Prefect) or "prefect" (opt-in;
            @task/@flow, run UI).
        on_event_factory: optional per-node event-printer factory (display label
            -> a per-event callback); the agent-node passes the NODE name as
            the label. Reaches each node via RunContext.on_event_factory. Kept
            here (build time) rather than in `params` because it is a callable
            (not serializable) and is engine plumbing, not a domain input.
        on_node_event: optional DAG-node lifecycle callback
            `(node_name, phase, status, agent) -> None`. Called with phase="start"
            (status=None) when a node begins and phase="finish" (status is the
            NodeOutcome status: "ok"/"degraded", or "failed" on a blocking error)
            when it ends — including on re-runs (a jumped-back node fires "start"
            again). `agent` is Node.agent (an informal display label; "" for
            hand-written nodes). Pure data (no rendering); a CLI turns it into a
            live view. Bound here at build time like on_event_factory (a
            non-serializable closure, engine plumbing not a domain input).
        run_instructions: optional run-wide brief injected into EVERY agent's
            prompt (e.g. a global directive from the CLI/start). Reaches each node
            via RunContext.run_instructions; an agent-node forwards it to
            run_agent.
        run_context: optional run-wide context SOURCES (file paths / globs)
            whose CONTENT is injected into every agent — rules/standards the
            agent must actually have. Read at run time (per node, so `{name}`
            templating works). Reaches each node via RunContext.run_context.
        agent_dir: optional DEFAULT directory where agent definitions live
            (opencode `--dir`); a node may override via agent_node(agent_dir=...).
            Reaches each node via RunContext.agent_dir. Templated at run time.
            Independent of run_dir (agents-here vs artifacts-there).
    """
    from agent_flow.backends import get_backend

    backend_impl = get_backend(backend, llm_tag=llm_tag)
    if registry is None:
        from agent_flow.registry import FlowRegistry

        registry = FlowRegistry()  # built-in gates only

    run_context_t = tuple(run_context or ())
    node_instructions_d = dict(node_instructions or {})
    durations_d = dict(durations or {})
    _check_durations(nodes, durations_d)
    planned = plan_groups(nodes)  # fail fast on cycles/unknown deps at build time
    by_name = {n.name: n for n in nodes}
    group_index = {key: i for i, (key, _) in enumerate(planned)}  # group key -> plan position
    node_group = {n.name: (n.parallel_group or n.name) for n in nodes}
    _emit = _make_node_emitter(on_node_event)

    def _make_run_node(wd: Path, params: dict, logger, pending: dict[str, str]) -> Callable[[str], Awaitable[NodeOutcome]]:
        """Build the backend-agnostic 'run ONE node' closure for this run.

        The returned closure is a COROUTINE function (the backend awaits it) —
        the same shape `backends.base.RunNode` names.

        Captures the resolved run_dir/params/logger. The backend calls it (inline
        or on N threads / Prefect tasks) but never decides what running a node
        MEANS — that is interpret(gate directives + criticality + bounded re-runs)
        plus start/finish emits and duration stamping, all here.

        `pending` is the run's {node_name: one-time-instruction} store: when the
        walker jumps back to a node it records the GoTo's instruction here, and
        this closure pops it (once) to hand the target node's re-run its one-time
        instruction. Empty for a normal forward run.
        """

        async def run_node(node_name: str) -> NodeOutcome:
            node = by_name[node_name]
            started = time.monotonic()
            # Pop this node's pending one-time instruction (from a prior cross-node
            # GoTo), if any — delivered to exactly this run, then gone.
            attempt_instruction = pending.pop(node_name, "")

            def _on_error(n: Node, exc: Exception) -> str:
                # Log only the first line (the real cause). The full detail —
                # command, sidecar parenthetical — travels on the raised
                # NodeBlocked and is printed once by the CLI, so we don't
                # duplicate the multi-line reason in the log.
                summary = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                if n.criticality == "blocking":
                    logger.error(f"BLOCKING node {n.name} failed: {summary}")
                    _emit(n.name, "finish", "failed", n.agent)
                    raise NodeBlocked(f"{n.name}: {exc}") from exc
                logger.warning(f"DEGRADE node {n.name} failed: {summary} — continuing")
                return "degraded"

            logger.info(f"node {node.name}: start (criticality={node.criticality})")
            _emit(node.name, "start", None, node.agent)
            # on_event_factory / run_instructions / run_context / agent_dir
            # are build-time values threaded into every node.
            outcome = await interpret(
                node,
                run_dir=wd,
                params=params,
                on_error=_on_error,
                log=logger.info,
                on_event_factory=on_event_factory,
                run_instructions=run_instructions,
                run_context=run_context_t,
                agent_dir=agent_dir,
                node_instructions=node_instructions_d,
                durations=durations_d,
                registry=registry,
                one_time_instruction=attempt_instruction,
            )
            # Stamp the node's wall-clock duration (timed here, where it runs).
            outcome = replace(outcome, duration_s=time.monotonic() - started)
            logger.info(f"node {node.name}: {outcome.status} ({outcome.duration_s:.1f}s)")
            _emit(node.name, "finish", outcome.status, node.agent)
            return outcome

        return run_node

    async def _pipeline(run_dir: str = "", start_from: str = "", only: str = "", **params: Any) -> dict:
        from agent_flow.run_context import init_run_context
        from agent_flow.utils import resolve_run_dir, resolve_template

        # Install the run-scoped domain-param store from the initial params.
        # Nodes read a snapshot (so upstream exports are visible) and export hooks
        # write to it. Same-process, run-scoped (see run_context SCOPE).
        init_run_context(params)
        # run_dir supports the same `{param}` templating as node inputs, but
        # STRICT: a path is never valid half-substituted, so a missing placeholder
        # is a hard error (not a dir literally named "{key}").
        try:
            resolved_run_dir = resolve_template(run_dir, params, strict=True)
        except KeyError as exc:
            raise ValueError(f"run_dir template references unknown param {exc}; available params: {sorted(params)}") from exc
        # Empty run_dir -> a fresh dir under <temp>/agent-flow/ (never cwd).
        wd = resolve_run_dir(resolved_run_dir, name=name)
        wd.mkdir(parents=True, exist_ok=True)

        async def _walk_session() -> dict[str, NodeOutcome]:
            # Runs INSIDE the backend's execution context (a Prefect @flow for the
            # prefect backend; directly for local) — so the logger, concurrency
            # limit, and node submission all bind to that context here.
            logger = backend_impl.get_logger()
            logger.info(f"run_dir: {wd}")
            if llm_concurrency is not None:
                await backend_impl.apply_concurrency_limit(llm_tag, llm_concurrency, logger.info, logger.warning)
            # Run-scoped {node: one-time instruction} store; the walker fills it on
            # a cross-node GoTo, run_node drains it into the target's re-run.
            pending_instructions: dict[str, str] = {}
            run_node = _make_run_node(wd, params, logger, pending_instructions)
            run_group = _make_group_runner(backend_impl, registry, run_node, logger)
            start_index, single_group = _resolve_entry(start_from, only, by_name, group_index, node_group, logger)
            results = await _walk(
                planned,
                run_group=run_group,
                group_index=group_index,
                node_group=node_group,
                by_name=by_name,
                logger=logger,
                start_index=start_index,
                single_group=single_group,
                pending_instructions=pending_instructions,
            )
            # Compact {node: status} summary — not the verbose NodeOutcome reprs.
            logger.info(f"{name} done: { {n: oc.status for n, oc in results.items()} }")
            return results

        backend_impl.bootstrap()
        try:
            return await backend_impl.run_session(name, _walk_session)
        finally:
            backend_impl.teardown()

    return _pipeline


def _resolve_entry(start_from: str, only: str, by_name, group_index, node_group, logger) -> tuple[int, bool]:
    """Resolve the walk's forward entry from the two mutually exclusive knobs.

    Returns (start_index, single_group): `only` -> (that group, True) so the walk
    runs exactly one group; `start_from` -> (that group, False) so it runs forward
    to the end; neither -> (0, False). Setting both is an error (they conflict).
    """
    if only and start_from:
        raise ValueError("only and start_from are mutually exclusive (only runs a single group; start_from runs from a group to the end)")
    if only:
        return _resolve_only_index(only, by_name, group_index, node_group, logger), True
    return _resolve_start_index(start_from, by_name, group_index, node_group, logger), False


def _name_to_group_index(target: str, by_name, group_index, node_group, kind: str) -> int:
    """Translate a NODE or parallel-GROUP name to its GROUP index.

    Shared by start_from and only: a name is either a node (resolved to its
    containing group) or a parallel-group name (used directly). `kind` labels the
    error/log so the two callers read distinctly. Unknown name -> ValueError.
    """
    if target in by_name:
        return group_index[node_group[target]]
    if target in group_index:  # a parallel-group name
        return group_index[target]
    known = sorted(set(by_name) | set(group_index))
    raise ValueError(f"{kind}={target!r} is not a known node or group (known: {known})")


def _resolve_start_index(start_from: str, by_name, group_index, node_group, logger) -> int:
    """Translate a start_from NODE name to its GROUP index (0 when unset).

    Forward entry point: the walk begins at the group CONTAINING start_from and
    proceeds forward. Granularity is the GROUP, not the node — a parallel group is
    the indivisible unit of execution, so if start_from is one member of a parallel
    group, the WHOLE group runs (you cannot enter "in the middle" of a fan-out).
    We log the entry group's members so that is visible, not surprising.

    `start_from` accepts either a NODE name or a parallel-GROUP name (the name you
    passed as agent_node(parallel_group=...)). A group name is the natural way to
    enter a fan-out; a member node name resolves to the same group.

    Skipping upstream assumes those nodes' side-effects (files on disk) and
    exported params already exist — the CALLER's responsibility (see docs).
    Runtime-populated params fall back to their defaults when their producer is
    skipped. Unknown name -> error.
    """
    if not start_from:
        return 0
    start_index = _name_to_group_index(start_from, by_name, group_index, node_group, "start_from")
    entry = sorted(n for n in by_name if group_index[node_group[n]] == start_index)
    skipped = sorted(n for n in by_name if group_index[node_group[n]] < start_index)
    if len(entry) > 1:
        logger.info(f"start_from={start_from}: entering at PARALLEL group {entry} (all run), skipping {skipped}")
    else:
        logger.info(f"start_from={start_from}: entering at {entry}, skipping {skipped}")
    return start_index


def _resolve_only_index(only: str, by_name, group_index, node_group, logger) -> int:
    """Translate an `only` NODE/GROUP name to the single GROUP index to run.

    Complement to start_from: run EXACTLY that one group and stop (see _walk's
    single_group). Same GROUP granularity as start_from — if `only` names a member
    of a parallel group, the WHOLE group runs (a fan-out is indivisible), so we log
    the group's members. Everything else (upstream AND downstream) is skipped;
    their outputs/exported params are assumed to already exist (caller's contract,
    same as start_from). Unknown name -> error.
    """
    idx = _name_to_group_index(only, by_name, group_index, node_group, "only")
    members = sorted(n for n in by_name if group_index[node_group[n]] == idx)
    if len(members) > 1:
        logger.info(f"only={only}: running PARALLEL group {members} (all run), skipping everything else")
    else:
        logger.info(f"only={only}: running {members}, skipping everything else")
    return idx


async def _walk(
    planned,
    *,
    run_group,
    group_index,
    node_group,
    by_name,
    logger,
    start_index: int = 0,
    single_group: bool = False,
    pending_instructions: dict[str, str] | None = None,
) -> dict[str, NodeOutcome]:
    """Walk the planned groups, honoring bounded cross-node jump-backs (GoTo).

    Returns per-node NodeOutcome (status + duration_s), so callers can render
    both. On a re-run (jump-back), a node's later outcome replaces the earlier.

    `start_index` is the FORWARD entry point (default 0 = the first group): the
    walk begins at that group, skipping earlier ones. This is orthogonal to
    jump-back — it sets where the walk STARTS; jump-back mutates position DURING
    the run. Node->index translation is the caller's job (this stays mechanical).

    `single_group` (the `only` mode) runs EXACTLY the group at start_index and
    stops: no forward advance to later groups, and gate GoTo jump-backs are
    ignored (there is nothing downstream to resume into). It is the surgical
    complement to start_from's "from here to the end".
    """
    results: dict[str, NodeOutcome] = {}
    jumps: dict[str, int] = {}
    i = start_index
    while i < len(planned):
        _key, group = planned[i]
        logger.debug(f"walk: group[{i}] {_key!r} -> nodes {[getattr(n, 'name', n) for n in group]}")
        outcomes = await run_group(group)
        for n_name, oc in outcomes.items():
            results[n_name] = oc
        if single_group:
            break  # `only` mode: run one group, ignore jump-backs and forward advance
        target = _pick_jump_back(outcomes, node_group, group_index, i, jumps, by_name, logger)
        if target is not None:
            jumps[target] = jumps.get(target, 0) + 1
            # Deliver the jumping node's one-time instruction to the target's re-run.
            if pending_instructions is not None:
                instr = _instruction_for_target(outcomes, target)
                if instr:
                    pending_instructions[target] = instr
            i = group_index[node_group[target]]  # rewind to the target's group
            continue
        i += 1
    return results


def _instruction_for_target(outcomes: dict[str, NodeOutcome], target: str) -> str:
    """The one-time instruction from the outcome whose GoTo chose `target` (or "")."""
    for oc in outcomes.values():
        if oc.goto == target and oc.instruction:
            return oc.instruction
    return ""


def _pick_jump_back(outcomes, node_group, group_index, current_i, jumps, by_name, logger) -> str | None:
    """From a group's outcomes, pick a valid bounded backward GoTo target (or None).

    Only backward jumps (to an earlier group) are honored, bounded per target by
    the target node's max_cycles. Forward/unknown/exhausted targets are ignored
    with a log so a gate mistake fails visibly rather than looping.
    """
    for oc in outcomes.values():
        target = oc.goto
        if target is None:
            continue
        if target not in by_name:
            logger.warning(f"GoTo target {target!r} is not a known node — ignoring")
            continue
        target_i = group_index[node_group[target]]
        if target_i >= current_i:
            logger.warning(f"GoTo {target!r} is not a backward jump — ignoring")
            continue
        if jumps.get(target, 0) >= by_name[target].max_cycles:
            logger.warning(f"GoTo {target!r} exhausted (max_cycles) — proceeding")
            continue
        logger.info(f"jump-back to {target!r} (re-running from its group)")
        return target
    return None
