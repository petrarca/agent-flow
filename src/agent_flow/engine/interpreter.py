"""Running ONE node to completion and interpreting its gate directive.

`interpret` is the unit of work the backends schedule: run the node's callable,
apply its gate, honour Restart/GoTo/Stop within the node's own re-run budget,
and settle to a `NodeOutcome`. Cross-node jump-back is the walker's job — this
module handles only the self-loop.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from agent_flow.engine.dispatch import maybe_await
from agent_flow.flow_types import Node, NodeBlocked, NodeOutcome, RunContext
from agent_flow.gates import Continue, GateContext, GoTo, Restart, Stop


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
    run_additional_instructions: str = "",
    run_context: tuple[str, ...] = (),
    agent_dir: str = "",
    node_overrides: dict[str, dict[str, Any]] | None = None,
    durations: dict[str, int] | None = None,
    options: dict[str, Any] | None = None,
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
            result = await maybe_await(
                node.run(
                    RunContext(
                        node=node,
                        run_dir=run_dir,
                        cycles=cycles,
                        params=eff_params,
                        on_event_factory=on_event_factory,
                        run_instructions=run_instructions,
                        run_additional_instructions=run_additional_instructions,
                        run_context=run_context,
                        agent_dir=agent_dir,
                        node_overrides=dict(node_overrides or {}),
                        durations=dict(durations or {}),
                        options=dict(options or {}),
                        one_time_instruction=attempt_instruction,
                        registry=registry,
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
            directive = await maybe_await(gate(gctx))
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
            await maybe_await(result)
    except Exception as exc:  # noqa: BLE001 - an observer must never break the run
        log(f"node {node.name}: {event} hook failed ({exc}) — ignored")


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
            derived = dict(await maybe_await(registry.get_export(node.export_ref)(payload)) or {})
        elif callable(spec):
            derived = dict(await maybe_await(spec(payload)) or {})
        elif isinstance(spec, dict):  # the declarative {param_name: result_field} map
            derived = {param: v for param, fld in spec.items() if (v := _read_field(payload, fld)) is not _MISSING}
        else:  # unreachable: the guard above returns when neither spec nor export_ref is set
            derived = {}
        if derived:
            get_run_context().update(derived)
            log(f"node {node.name}: exported {sorted(derived)} to run-context")
    except Exception as exc:  # noqa: BLE001 - exports are consumer code; never fail the run
        log(f"node {node.name}: exports failed ({exc}) — ignored")
