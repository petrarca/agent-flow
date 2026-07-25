"""Batteries — the one-call helper for the common case: a node that runs ONE agent.

Layer 3's `Node` takes a `run` callable, which is maximally flexible but means a
consumer hand-writes prompt-building, control-path derivation, and the run_agent
call for every node. The overwhelmingly common shape is simply: "run one runtime
agent, hand it a work order of KEY: value inputs, point it at a control file, get
the result." `agent_node(...)` builds exactly that node in one call.

It is a CONVENIENCE, not a new layer: it returns a plain `Node`, so it composes
with hand-written `run` callables in the same graph. Domain-neutral by design —
there is NO notion of "analyst"/"verifier" here. A verifier is just another
`agent_node` that `depends_on` its subject and carries a `rerun_on_signal(...)`
gate; the engine's bounded `GoTo` drives the re-run. Any node can route flow to
any upstream node — the library imposes no adjacency.

This module is the one place allowed to depend on BOTH the engine (Node) and the
Layer-1 core (run_agent) — keeping `engine.py` itself decoupled from the runtime.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_flow.agent_runtime import DEFAULT_IDLE_TIMEOUT_S, run_agent
from agent_flow.engine import Criticality, Node, RunContext
from agent_flow.gates import Gate
from agent_flow.runners import get_runner
from agent_flow.utils import resolve_template


def control_path(node_name: str) -> str:
    """The per-node control-sidecar filename (node name is unique per run)."""
    return f"{node_name}.control.json"


def _node_logger():
    """Prefect's run logger inside a flow/task, else the stdlib logger.

    Using Prefect's logger (when available) makes per-node lines appear in the
    same formatted stream as the engine's node logs; standalone (Tier-1/2 without
    a Prefect run context) it degrades to the plain logger.
    """
    import logging

    try:
        from prefect.logging import get_run_logger

        return get_run_logger().info
    except Exception:  # noqa: BLE001 - no active run context (or Prefect absent) -> stdlib
        return logging.getLogger("agent_flow").info


def build_work_order(inputs: dict[str, str], params: dict[str, Any]) -> str:
    """Render the KEY: value work-order prompt from templated inputs.

    Each value may reference run params via `{name}` (e.g. "{product_key}"); the
    library also exposes nothing implicitly — the consumer decides the keys. The
    completion protocol (CONTROL_FILE + control JSON shape) is injected separately
    by run_agent, so it is NOT part of these inputs.
    """
    return "\n".join(f"{key}: {resolve_template(val, params)}" for key, val in inputs.items())


def agent_node(
    name: str,
    agent: str,
    *,
    inputs: dict[str, str] | None = None,
    depends_on: tuple[str, ...] = (),
    parallel_group: str | None = None,
    criticality: Criticality = "blocking",
    instructions: str = "",
    context: tuple[str, ...] = (),
    gate: Gate | None = None,
    result_schema: object = None,
    max_cycles: int = 1,
    model: str | None = None,
    idle_timeout_s: int | None = None,
    agent_dir: str | None = None,
) -> Node:
    """Build a `Node` that runs ONE runtime agent as a supervised subprocess.

    Args:
        name: node id (also the control-sidecar base name).
        agent: the runtime agent to dispatch (e.g. an opencode `--agent` name).
            Domain-neutral: the library attaches no meaning to it.
        inputs: KEY: value work-order handed to the agent. Values may template
            run params via `{name}`. Report/output paths, product keys, etc. go
            here — this is how you "pass the agent what it needs". Absolute paths
            are recommended for files (opencode resolves relative paths against
            its project root, not the subprocess cwd).
        instructions: optional per-node instruction block, ADDITIVE to the
            always-injected control protocol and any run-wide shared_instructions.
            "For this node specifically, also do X." May template run params via
            `{name}`. Prepended to the KEY: value work order.
        context: optional per-node context SOURCES (file paths / globs) whose
            CONTENT is read and injected for THIS node — rules/standards this
            step must actually have. May template run params. Injected before the
            per-node instructions (which come before the work order).
        depends_on / parallel_group / criticality / max_cycles: DAG wiring +
            flow-control knobs (see engine.Node).
        gate: optional consumer gate (see gates). Absent means always Continue.
        result_schema: optional ResultSchema | JSON-schema dict | pydantic
            BaseModel subclass for the agent's `result` payload (injected +
            validated, never fails the run).
        model / idle_timeout_s: per-node runtime overrides.
        agent_dir: optional per-node override of where agent DEFINITIONS live
            (opencode `--dir`). Defaults to the flow's build_flow(agent_dir=...).
            Templated; absolute after templating.

    Returns a plain `Node`, so it mixes freely with hand-written `run` nodes.
    """
    inputs = inputs or {}

    def run(ctx: RunContext) -> dict:
        import logging

        from agent_flow.context import read_context_blocks

        warn = logging.getLogger("agent_flow").warning
        control_abs = ctx.run_dir / control_path(name)
        # Expose the run_dir to input templates as {run_dir}, alongside params.
        tmpl = {**ctx.params, "run_dir": str(ctx.run_dir)}
        work_order = build_work_order(inputs, tmpl)

        # The caller-visible prompt, composed in order:
        #   [per-node context content] [per-node instructions] [work order]
        # (Run-wide context + instructions are prepended by run_agent.)
        parts: list[str] = []
        node_ctx = read_context_blocks(context, params=ctx.params, run_dir=ctx.run_dir, warn=warn)
        if node_ctx:
            parts.append(f"## Context for this step\n\n{node_ctx}")
        if instructions and instructions.strip():
            parts.append(f"## Instructions for this step\n\n{resolve_template(instructions, tmpl)}")
        parts.append(work_order)
        prompt = "\n\n".join(parts)

        # Read the run-wide context SOURCES into content here (per node, so
        # templating works) and hand the content to run_agent.
        shared_ctx = read_context_blocks(ctx.shared_context, params=ctx.params, run_dir=ctx.run_dir, warn=warn)

        # Per-node agent_dir overrides the flow default; both are templated.
        eff_agent_dir = resolve_template(agent_dir, tmpl) if agent_dir else resolve_template(ctx.agent_dir, tmpl)

        runtime = ctx.params.get("runtime", "opencode")
        # A per-node model (agent_node arg) wins; else the run-wide model from
        # params. Empty ("") means "no model" -> the runner omits --model and the
        # runtime resolves it. The library never substitutes a hardcoded model.
        eff_model = model or ctx.params.get("model") or ""
        # idle_timeout_s: a per-node override (agent_node arg) wins; else the
        # run-wide value from params (RunConfig / CLI --idle-timeout); else the
        # library default. No number is hardcoded on the node.
        eff_idle = int(idle_timeout_s if idle_timeout_s is not None else (ctx.params.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S))
        log = _node_logger()
        log("node %s: agent=%s runtime=%s model=%s idle_timeout_s=%s", name, agent, runtime, eff_model, eff_idle)
        # on_event_factory is a typed RunContext field (engine plumbing), NOT a
        # params key — see RunContext.on_event_factory. We build the per-event
        # callback with the NODE name (not the agent): the node is the DAG unit
        # the reader navigates by, and it may differ from the agent that
        # implements it (agent is an impl detail). In a firehose of live lines,
        # the node label is what tells you where in the flow you are.
        make_printer = ctx.on_event_factory
        result = run_agent(
            agent=agent,
            prompt=prompt,
            run_dir=ctx.run_dir,
            agent_dir=Path(eff_agent_dir) if eff_agent_dir else None,
            runner=get_runner(runtime),
            idle_timeout_s=eff_idle,
            model=eff_model,
            control_file=control_abs,
            result_schema=result_schema,
            on_event=make_printer(name) if callable(make_printer) else None,
            shared_instructions=ctx.shared_instructions,
            shared_context=shared_ctx,
        )
        log(
            "node %s: agent=%s -> %s in %.1fs (tokens=%d cost=$%.4f completion=%s)",
            name,
            agent,
            result.control.get("status"),
            result.duration_s,
            result.tokens,
            result.cost,
            result.completion,
        )
        # Hand the gate the control envelope plus a little telemetry, under keys
        # unlikely to collide with the agent's own result fields. `_result_obj`
        # is the VALIDATED typed object (a Pydantic model instance when a
        # PydanticSchema was used) so a gate can decide on typed fields directly,
        # e.g. ctx.result["_result_obj"].languages. None when no schema was given.
        return {
            **result.control,
            "_tokens": result.tokens,
            "_cost": result.cost,
            "_completion": result.completion,
            "_result_valid": result.result_valid,
            "_result_errors": list(result.result_errors),
            "_result_obj": result.result_obj,
        }

    return Node(
        name=name,
        run=run,
        gate=gate,
        depends_on=depends_on,
        parallel_group=parallel_group,
        criticality=criticality,
        max_cycles=max_cycles,
        result_schema=result_schema,
        agent=agent,
    )
