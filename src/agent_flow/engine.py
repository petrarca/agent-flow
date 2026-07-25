"""Declaration-driven engine — compiles a Node graph into a Prefect flow.

This is the library's Layer-3 API: declare a pipeline as a list of `Node`s and
`build_flow` returns a runnable Prefect flow that walks the DAG, fans out
parallel groups, invokes each node's gate, and interprets the returned directive
(Continue / Restart / GoTo / Stop) with bounded re-run cycles and per-node
criticality.

The engine is deliberately AGNOSTIC to what a node does. It knows nothing about
"analysts", "verifiers", reports, or prompts — a `Node` carries a `run` callable
that performs the actual work (build a prompt, call `run_agent`, run a composite
of several agents, whatever). The engine only orchestrates: order, parallelism,
gate directives, criticality. Domain knowledge lives in `run` and `gate`.

Two layers of use:
  - Layer 3 (this module): declare Nodes, call `build_flow`. Prefect is hidden.
  - Layer 2 (no this module): write your own Prefect flow and call `run_agent`
    directly as a leaf. `build_flow` is optional, not required.

The DAG walk and gate interpretation are factored into pure helpers
(`plan_groups`, `interpret`) that take a `submit` callable, so the orchestration
logic is unit-testable without a Prefect server.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

from agent_flow.gates import Continue, Gate, GateContext, GoTo, Restart, Stop

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
             LABEL (the batteries node passes the NODE name — the DAG unit the
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
    shared_instructions: str = ""
    # Run-wide context SOURCES (file paths / globs) whose CONTENT is injected
    # into every agent — rules/standards the agent must actually have. Read at
    # run time (per node, so templating against params works). Build-time
    # plumbing, not a domain param.
    shared_context: tuple[str, ...] = ()
    # Run-time per-node instructions {node_name: text}, from CLI --instruct / the
    # config node_instructions: section. A batteries node appends its own entry
    # LAST (after the build-time per-node instructions), so it is the most recent
    # standing guidance before the work order — additive, last-word override.
    node_instructions: dict[str, str] = field(default_factory=dict)


# A node's work: perform the invocation, return whatever the gate will inspect
# (typically the control dict or an AgentResult). May raise on hard failure.
RunFn = Callable[[RunContext], Any]


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
    agent: str = ""
    # Optional result->params export hook. After the node completes (and is not
    # re-running), the engine derives keys from the node's result and merges them
    # into the run-context service so DOWNSTREAM nodes template against them.
    # Either a callable `(result) -> Mapping[str, Any]`, or a declarative map
    # `{param_name: result_field}`. See agent_node(exports=...) and run_context.
    exports: Callable[[dict[str, Any]], Any] | dict[str, str] | None = None


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


def plan_groups(nodes: Iterable[Node]) -> list[tuple[str, list[Node]]]:
    """Order nodes into execution groups honoring depends_on and parallel_group.

    Returns a list of (group_key, nodes) in a valid execution order: every
    group's dependencies appear in an earlier group. Nodes sharing a
    parallel_group run together; solo nodes are their own single-member group.

    Raises ValueError on an unknown dependency or a dependency cycle.
    """
    nodes = list(nodes)
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
    duration_s   wall-clock seconds the node took (set by build_flow's task; 0.0
                 when produced by interpret directly, e.g. in unit tests).
    """

    status: str
    goto: str | None = None
    duration_s: float = 0.0


def _make_node_emitter(on_node_event: Callable[[str, str, str | None, str], None] | None) -> Callable[[str, str, str | None, str], None]:
    """Return a node-lifecycle emitter: the user callback, or a no-op if None.

    Keeps the None-guard out of build_flow's body (one bound callable there),
    so build_flow stays under the complexity limit and the emit sites read as
    unconditional calls.
    """
    if on_node_event is None:
        return lambda _name, _phase, _status, _agent: None
    return on_node_event


def interpret(
    node: Node,
    *,
    run_dir: Path,
    params: dict[str, Any],
    on_error: Callable[[Node, Exception], str],
    log: Callable[[str], None] = lambda _msg: None,
    on_event_factory: Callable[[str], Any] | None = None,
    shared_instructions: str = "",
    shared_context: tuple[str, ...] = (),
    agent_dir: str = "",
    node_instructions: dict[str, str] | None = None,
) -> NodeOutcome:
    """Run one node to completion, interpreting its gate's directives.

    Returns a NodeOutcome. Raises NodeBlocked when a blocking node fails or its
    gate returns Stop. Restart (and GoTo-to-self) re-run the node in place,
    bounded by max_cycles. A cross-node GoTo(other) is NOT handled here — it is
    surfaced via NodeOutcome.goto for the walker to act on (jump-back).

    `on_error` maps a raised exception to a status per criticality (and may
    itself raise NodeBlocked).
    """
    from agent_flow.run_context import get_run_context

    cycles = 0
    while True:
        # Effective params = the passed params overlaid with the live run-context
        # snapshot, so this node sees any values UPSTREAM nodes exported. Snapshot
        # is taken at THIS node's start -> a stable view for its whole execution.
        eff_params = {**params, **get_run_context().snapshot()}
        try:
            result = node.run(
                RunContext(
                    node=node,
                    run_dir=run_dir,
                    cycles=cycles,
                    params=eff_params,
                    on_event_factory=on_event_factory,
                    shared_instructions=shared_instructions,
                    shared_context=shared_context,
                    agent_dir=agent_dir,
                    node_instructions=dict(node_instructions or {}),
                )
            )
        except Exception as exc:  # noqa: BLE001 - deliberately broad: the run callable is caller code
            return NodeOutcome(status=on_error(node, exc))

        # The validated typed object (a pydantic instance when a result_schema was
        # set, else None) — surfaced as GateContext.obj so gates/exports read it
        # directly instead of digging the `_result_obj` key out of `result`.
        obj = result.get("_result_obj") if isinstance(result, dict) else None
        directive = (
            node.gate(GateContext(result=result, obj=obj, node=node, run_dir=run_dir, cycles=cycles, params=eff_params, agent_dir=agent_dir))
            if node.gate
            else Continue()
        )

        if isinstance(directive, Stop):
            log(f"node {node.name}: gate -> Stop ({directive.reason})")
            raise NodeBlocked(directive.reason or node.name)

        restart = isinstance(directive, Restart) or (isinstance(directive, GoTo) and directive.node == node.name)
        if restart and cycles < node.max_cycles:
            cycles += 1
            note = getattr(directive, "note", "")
            log(f"node {node.name}: gate -> re-run cycle {cycles} ({note})")
            continue

        # Node is settling (Continue / cross-node GoTo / exhausted Restart): apply
        # its result->params exports to the run-context for DOWNSTREAM nodes.
        _apply_exports(node, result, obj, log)

        if isinstance(directive, GoTo) and directive.node != node.name:
            # Cross-node jump-back: the node itself is done; the walker decides
            # whether to rewind to the target (bounded there).
            log(f"node {node.name}: gate -> GoTo {directive.node} ({directive.note})")
            return NodeOutcome(status="ok", goto=directive.node)

        # Continue, or an exhausted Restart.
        return NodeOutcome(status="ok")


_MISSING = object()


def _read_field(src: Any, field: str) -> Any:
    """Read `field` from a dict (by key) or an object (by attribute); _MISSING if absent."""
    if isinstance(src, dict):
        return src.get(field, _MISSING)
    return getattr(src, field, _MISSING)


def _apply_exports(node: Node, result: Any, obj: Any, log: Callable[[str], None]) -> None:
    """Merge a node's result-derived exports into the run-context service.

    The consumer's `node.exports` sees the VALIDATED typed object when the node
    declared a `result_schema` (a pydantic instance), else the raw result dict —
    one payload, no signature sniffing. Two forms:
      - callable `(payload) -> Mapping` — full control.
      - declarative `{param_name: field}` — copy fields (attribute or dict key).
    Missing fields are skipped. Never raises: a mistake logs and is ignored.
    """
    spec = node.exports
    if not spec:
        return
    from agent_flow.run_context import get_run_context

    payload = obj if obj is not None else result
    try:
        if callable(spec):
            derived = dict(spec(payload) or {})
        else:
            derived = {param: v for param, fld in spec.items() if (v := _read_field(payload, fld)) is not _MISSING}
        if derived:
            get_run_context().update(derived)
            log(f"node {node.name}: exported {sorted(derived)} to run-context")
    except Exception as exc:  # noqa: BLE001 - exports are consumer code; never fail the run
        log(f"node {node.name}: exports failed ({exc}) — ignored")


def build_flow(
    nodes: list[Node],
    *,
    name: str = "agent-flow",
    llm_tag: str = "llm",
    llm_concurrency: int | None = None,
    on_event_factory: Callable[[str], Any] | None = None,
    on_node_event: Callable[[str, str, str | None, str], None] | None = None,
    shared_instructions: str = "",
    shared_context: Iterable[str] | None = None,
    agent_dir: str = "",
    node_instructions: dict[str, str] | None = None,
):
    """Compile a Node graph into a runnable Prefect flow.

    Returns a Prefect `@flow`-decorated callable `f(run_dir, **params) -> dict`
    that walks the DAG (`plan_groups`), fans out parallel groups concurrently,
    and runs each node via `interpret` (gate directives + criticality + bounded
    re-runs). `params` are threaded unchanged into every node's RunContext.

    Prefect is imported lazily HERE (not at module import) so the engine module
    can be imported before the app calls `_prefect_env.bootstrap()`.

    Args:
        nodes: the pipeline DAG.
        name: Prefect flow name.
        llm_tag: concurrency tag applied to each node task (for a shared limit).
        llm_concurrency: if set, a global concurrency limit on `llm_tag`.
        on_event_factory: optional per-node event-printer factory (display label
            -> a per-event callback); the batteries node passes the NODE name as
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
        shared_instructions: optional run-wide brief injected into EVERY agent's
            prompt (e.g. a global directive from the CLI/start). Reaches each node
            via RunContext.shared_instructions; a batteries node forwards it to
            run_agent.
        shared_context: optional run-wide context SOURCES (file paths / globs)
            whose CONTENT is injected into every agent — rules/standards the
            agent must actually have. Read at run time (per node, so `{name}`
            templating works). Reaches each node via RunContext.shared_context.
        agent_dir: optional DEFAULT directory where agent definitions live
            (opencode `--dir`); a node may override via agent_node(agent_dir=...).
            Reaches each node via RunContext.agent_dir. Templated at run time.
            Independent of run_dir (agents-here vs artifacts-there).
    """
    from prefect import flow, get_run_logger, task
    from prefect.futures import wait

    shared_context_t = tuple(shared_context or ())
    node_instructions_d = dict(node_instructions or {})
    planned = plan_groups(nodes)  # fail fast on cycles/unknown deps at build time
    by_name = {n.name: n for n in nodes}
    group_index = {key: i for i, (key, _) in enumerate(planned)}  # group key -> plan position
    node_group = {n.name: (n.parallel_group or n.name) for n in nodes}
    _emit = _make_node_emitter(on_node_event)

    @task(tags=[llm_tag])
    def _node_task(node_name: str, run_dir: str, params: dict) -> NodeOutcome:
        node = by_name[node_name]
        logger = get_run_logger()

        started = time.monotonic()

        def _on_error(n: Node, exc: Exception) -> str:
            if n.criticality == "blocking":
                logger.error("BLOCKING node %s failed: %s", n.name, exc)
                _emit(n.name, "finish", "failed", n.agent)
                raise NodeBlocked(f"{n.name}: {exc}") from exc
            logger.warning("DEGRADE node %s failed: %s — continuing", n.name, exc)
            return "degraded"

        # Name the node in the log (Prefect's own task-run id is opaque).
        logger.info("node %s: start (criticality=%s)", node.name, node.criticality)
        _emit(node.name, "start", None, node.agent)
        # on_event_factory is a build-time closure (not serializable), so it is
        # bound here — never passed through the task's `params` arg.
        # shared_instructions / shared_context / agent_dir are likewise build-time
        # values threaded into every node.
        outcome = interpret(
            node,
            run_dir=Path(run_dir),
            params=params,
            on_error=_on_error,
            log=logger.info,
            on_event_factory=on_event_factory,
            shared_instructions=shared_instructions,
            shared_context=shared_context_t,
            agent_dir=agent_dir,
            node_instructions=node_instructions_d,
        )
        # Stamp the node's wall-clock duration (timed here, where the node runs).
        outcome = replace(outcome, duration_s=time.monotonic() - started)
        logger.info("node %s: %s (%.1fs)", node.name, outcome.status, outcome.duration_s)
        _emit(node.name, "finish", outcome.status, node.agent)
        return outcome

    def _run_group(group: list[Node], wd: Path, params: dict, logger) -> dict[str, NodeOutcome]:
        """Execute one group (solo inline, multi via submit+wait). Returns per-node outcomes."""
        if len(group) == 1:
            n = group[0]
            return {n.name: _node_task(n.name, str(wd), params)}
        logger.info("PARALLEL group: %s", [n.name for n in group])
        futures = [_node_task.submit(n.name, str(wd), params) for n in group]
        wait(futures)
        out: dict[str, NodeOutcome] = {}
        for n, fut in zip(group, futures, strict=True):
            out[n.name] = fut.result() if fut.state.is_completed() else NodeOutcome(status="degraded")
        return out

    @flow(name=name)
    def _pipeline(run_dir: str = "", start_from: str = "", **params: Any) -> dict:
        from agent_flow.run_context import init_run_context
        from agent_flow.utils import resolve_run_dir

        # Install the run-scoped domain-param store from the initial params. Nodes
        # read a snapshot of it (so upstream exports are visible) and export hooks
        # write to it. Same-process, run-scoped (see run_context module SCOPE).
        init_run_context(params)

        logger = get_run_logger()
        # run_dir supports the same `{param}` templating as node inputs, but
        # STRICT: a path is never valid half-substituted, so a missing placeholder
        # is a hard error (not a dir literally named "{product_key}").
        from agent_flow.utils import resolve_template

        try:
            run_dir = resolve_template(run_dir, params, strict=True)
        except KeyError as exc:
            raise ValueError(f"run_dir template references unknown param {exc}; available params: {sorted(params)}") from exc
        # Empty run_dir -> a fresh dir under <temp>/agent-flow/ (never litter cwd).
        wd = resolve_run_dir(run_dir, name=name)
        wd.mkdir(parents=True, exist_ok=True)
        logger.info("run_dir: %s", wd)
        if llm_concurrency is not None:
            _apply_concurrency_limit(llm_tag, llm_concurrency, logger.info, logger.warning)
        start_index = _resolve_start_index(start_from, by_name, group_index, node_group, logger)
        results = _walk(
            planned,
            run_group=lambda group: _run_group(group, wd, params, logger),
            group_index=group_index,
            node_group=node_group,
            by_name=by_name,
            logger=logger,
            start_index=start_index,
        )
        # Log a compact {node: status} summary — not the verbose NodeOutcome reprs.
        logger.info("%s done: %s", name, {n: oc.status for n, oc in results.items()})
        return results

    return _pipeline


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
    # Accept a node name OR a group name (node_group values are the group keys).
    if start_from in by_name:
        group_key = node_group[start_from]
    elif start_from in group_index:  # a parallel-group name
        group_key = start_from
    else:
        known = sorted(set(by_name) | set(group_index))
        raise ValueError(f"start_from={start_from!r} is not a known node or group (known: {known})")
    start_index = group_index[group_key]
    entry = sorted(n for n in by_name if group_index[node_group[n]] == start_index)
    skipped = sorted(n for n in by_name if group_index[node_group[n]] < start_index)
    if len(entry) > 1:
        logger.info("start_from=%s: entering at PARALLEL group %s (all run), skipping %s", start_from, entry, skipped)
    else:
        logger.info("start_from=%s: entering at %s, skipping %s", start_from, entry, skipped)
    return start_index


def _walk(planned, *, run_group, group_index, node_group, by_name, logger, start_index: int = 0) -> dict[str, NodeOutcome]:
    """Walk the planned groups, honoring bounded cross-node jump-backs (GoTo).

    Returns per-node NodeOutcome (status + duration_s), so callers can render
    both. On a re-run (jump-back), a node's later outcome replaces the earlier.

    `start_index` is the FORWARD entry point (default 0 = the first group): the
    walk begins at that group, skipping earlier ones. This is orthogonal to
    jump-back — it sets where the walk STARTS; jump-back mutates position DURING
    the run. Node->index translation is the caller's job (this stays mechanical).
    """
    results: dict[str, NodeOutcome] = {}
    jumps: dict[str, int] = {}
    i = start_index
    while i < len(planned):
        _key, group = planned[i]
        outcomes = run_group(group)
        for n_name, oc in outcomes.items():
            results[n_name] = oc
        target = _pick_jump_back(outcomes, node_group, group_index, i, jumps, by_name, logger)
        if target is not None:
            jumps[target] = jumps.get(target, 0) + 1
            i = group_index[node_group[target]]  # rewind to the target's group
            continue
        i += 1
    return results


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
            logger.warning("GoTo target %r is not a known node — ignoring", target)
            continue
        target_i = group_index[node_group[target]]
        if target_i >= current_i:
            logger.warning("GoTo %r is not a backward jump — ignoring", target)
            continue
        if jumps.get(target, 0) >= by_name[target].max_cycles:
            logger.warning("GoTo %r exhausted (max_cycles) — proceeding", target)
            continue
        logger.info("jump-back to %r (re-running from its group)", target)
        return target
    return None


def _apply_concurrency_limit(tag: str, limit: int, info, warn) -> None:
    """Best-effort global concurrency limit on a task tag (idempotent)."""
    import anyio
    import httpx
    from prefect.client.orchestration import get_client
    from prefect.exceptions import PrefectException

    async def _create() -> None:
        async with get_client() as client:
            await client.create_concurrency_limit(tag=tag, concurrency_limit=limit)

    # A pre-existing limit or a transient client error must not fail the run.
    try:
        anyio.run(_create)
        info(f"LLM concurrency limit set to {limit}")
    except (PrefectException, httpx.HTTPError, OSError) as exc:
        warn(f"concurrency limit setup skipped: {exc}")
