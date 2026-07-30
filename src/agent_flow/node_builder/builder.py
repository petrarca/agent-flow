"""`agent_node` — build a Node that runs ONE runtime agent.

The bridge between Tier 3 (the engine, which knows only `Node.run`) and Tier 1
(an executor supervising an agent). It is the only non-facade module that
imports both sides.

`agent_node` returns a plain `Node` whose `run` closure, per attempt: resolves
the work order, composes the prompt channels, resolves the per-node settings
(`resolve.py`), picks an executor (`executor_choice.py`), runs it, and returns
the control envelope for the gate to inspect.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_flow.flow_types import Criticality, Node, RunContext
from agent_flow.gates import Gate
from agent_flow.node_builder.executor_choice import select_executor
from agent_flow.node_builder.resolve import resolve_node_settings
from agent_flow.node_builder.work_order import DEFAULT_WORK_ORDER_RENDERER, _validate_inputs, resolve_work_order
from agent_flow.runners.base import AgentInvocation, PromptParts, render_prompt
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
    try:
        from prefect.logging import get_run_logger

        return get_run_logger().info
    except Exception:  # noqa: BLE001 - no active run context (or Prefect absent) -> loguru
        from loguru import logger

        return logger.info


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
    gate_ref: str | None = None,
    gate_args: dict[str, Any] | None = None,
    result_schema: object = None,
    input_schema: object = None,
    max_cycles: int = 1,
    model: str | None = None,
    duration: str | None = None,
    agent_dir: str | None = None,
    exports: Callable[[dict], Any] | dict[str, str] | None = None,
    export_ref: str | None = None,
    impl: Callable[..., Any] | None = None,
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
            always-injected control protocol and any run-wide run_instructions.
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
        model: per-node runtime override.
        duration: how long this node is EXPECTED to take, as a name from the
            duration vocabulary ("short"/"normal"/"long", or any name the run
            config's `durations:` defines). Portable INTENT — the run config maps
            it to concrete seconds. Unset means the run-wide idle timeout. An
            unknown name is a hard error at BUILD time (build_flow validates the
            whole graph), never a silent fallback.
        agent_dir: optional per-node override of where agent DEFINITIONS live
            (opencode `--dir`). Defaults to the flow's build_flow(agent_dir=...).
            Templated; absolute after templating.
        exports: optional result->params publish hook. After this node completes
            (and is not re-running), the engine derives keys from the node's result
            and merges them into the run-context service, so DOWNSTREAM nodes
            template `{key}` against them. The hook sees the VALIDATED typed object
            when the node set a `result_schema` (a pydantic instance), else the raw
            result dict — one payload, no signature sniffing. Two forms:
              - declarative `{param_name: field}` — copy fields (attribute or dict
                key) into params under (possibly renamed) keys; missing skipped.
              - callable `(payload) -> Mapping[str, Any]` — full control.
            Use it to route a value a node DISCOVERS (e.g. a readiness check's
            captured provenance or a chosen mode) to the agents that follow.
            Same-process, downstream-only; never targets parallel-group siblings
            (see run_context module SCOPE).
        impl: optional IN-PROCESS agent implementation — a callable
            `(AgentInvocation) -> AgentResult | pydantic model | dict`. When set,
            the node runs the agent as a direct Python call via InProcessExecutor
            (no subprocess, no control sidecar) instead of spawning `runtime`; it
            receives the same neutral invocation a subprocess agent would (the
            composed prompt, model, run_dir, result_schema, ...). `agent` remains
            the display label. The declarative equivalent is NodeDef.impl_ref +
            registry.agent_impl(name). See runners/inprocess.py.
    The FlowRegistry is NOT a parameter: it is run-scoped and arrives on the
    RunContext, from `build_flow(registry=...)`. It carries the `mock_agent`
    behaviours plus any gate / export / schema / renderer registrations. When
    `--mock-agents` mode is on and the registry holds a `mock_agent` for this
    node's `agent` name, the node runs that behaviour through MockExecutor — no
    subprocess, no tokens. Mocks are keyed by AGENT name, not by node, so one
    registration covers every node running that agent; with no matching entry
    the node runs normally (partial mock). Mock is not a runtime. See
    runners/mock_exec.py and docs design/mock-agent.md.

    Returns a plain `Node`, so it mixes freely with hand-written `run` nodes.
    """
    inputs = inputs or {}

    async def run(ctx: RunContext) -> dict:
        registry = ctx.registry
        from loguru import logger

        from agent_flow.core import read_context_blocks

        warn = logger.warning
        # This node's per-node run-config overrides (a plain dict; may be empty).
        # Read once here; each `ov.get(...)` below is the most-specific source in
        # its setting's precedence chain.
        ov = ctx.node_overrides.get(name, {})
        # Expose the run_dir to input templates as {run_dir}, alongside params.
        tmpl = {**ctx.params, "run_dir": str(ctx.run_dir)}
        resolved_inputs = resolve_work_order(inputs, tmpl)
        # Typed INPUTS (the mirror of result_schema): validate the RESOLVED work
        # order — after templating and upstream exports — so a missing param or an
        # unresolved `{export}` fails HERE with a real schema error, instead of
        # silently handing an agent a literal "{mode}". Raising routes through the
        # node's criticality like any other node error (blocking halts, degrade
        # degrades). `input_obj` is the typed instance for an in-process impl.
        input_obj = _validate_inputs(name, input_schema, resolved_inputs)
        # Rendered through the registry's work-order renderer (default: XML
        # tags). One code path — no inline duplicate of the shipped renderers.
        render = registry.get_work_order_renderer() if registry is not None else DEFAULT_WORK_ORDER_RENDERER
        work_order = render(resolved_inputs)

        # Collect the prompt's CHANNELS, each still separate, and hand them to a
        # renderer. Nothing is pre-joined here: a consumer's `registry.prompt`
        # override needs the parts, not a finished string (and an in-process impl
        # can read them off the invocation). The completion protocol is NOT a part
        # — it belongs to the runner, which prepends it after rendering.
        #
        # Run-wide context SOURCES are read into CONTENT here (per node, so
        # `{param}` templating in a path works).
        parts = PromptParts(
            run_context=read_context_blocks(ctx.run_context, params=ctx.params, run_dir=ctx.run_dir, warn=warn),
            run_instructions=ctx.run_instructions,
            # This run's ADDITIONAL run-wide brief (-i / config instructions),
            # after the flow's STANDING run_instructions — additive, not replacing.
            run_additional_instructions=ctx.run_additional_instructions,
            node_context=read_context_blocks(context, params=ctx.params, run_dir=ctx.run_dir, warn=warn),
            node_instructions=resolve_template(instructions, tmpl) if instructions else "",
            # Run-time per-node instruction (CLI --instruct / config
            # nodes.<n>.instructions): AFTER the build-time one, so it is the last
            # standing guidance — additive, last-word override.
            node_runtime_instructions=resolve_template(str(ov.get("instructions") or ""), tmpl),
            # One-time instruction for THIS attempt, set by the engine from a
            # gate's Restart/GoTo. Rendered VERBATIM (no library heading) and last
            # before the work order — the freshest, most specific guidance, whose
            # framing the gate owns. Ephemeral: the engine clears it afterwards.
            attempt_instruction=resolve_template(getattr(ctx, "one_time_instruction", "") or "", tmpl),
            work_order=work_order,
            inputs=dict(resolved_inputs),
        )
        render_body = registry.get_prompt_renderer() if registry is not None else render_prompt
        prompt = render_body(parts)
        shared_ctx = parts.run_context

        st = resolve_node_settings(name=name, ctx=ctx, ov=ov, tmpl=tmpl, agent_dir=agent_dir, model=model, duration=duration)
        runtime, eff_agent_dir, eff_model, eff_duration, eff_idle = st.runtime, st.agent_dir, st.model, st.duration, st.idle_timeout_s
        log = _node_logger()
        log(f"node {name}: agent={agent} runtime={runtime} model={eff_model} duration={eff_duration or '(run-wide)'} idle_timeout_s={eff_idle}")
        # on_event_factory is a typed RunContext field (engine plumbing), NOT a
        # params key — see RunContext.on_event_factory. We build the per-event
        # callback with the NODE name (not the agent): the node is the DAG unit
        # the reader navigates by, and it may differ from the agent that
        # implements it (agent is an impl detail). In a firehose of live lines,
        # the node label is what tells you where in the flow you are.
        make_printer = ctx.on_event_factory
        # Build the neutral invocation once; the executor decides HOW to run it.
        inv = AgentInvocation(
            agent=agent,
            prompt=prompt,
            run_dir=ctx.run_dir,
            node=name,
            result_schema=result_schema,
            model=eff_model,
            agent_dir=eff_agent_dir or "",
            run_instructions=ctx.run_instructions,
            run_context=shared_ctx,
            idle_timeout_s=eff_idle,
            on_event=make_printer(name) if callable(make_printer) else None,
            # The structured twin of `prompt`, for in-process impls that want the
            # VALUES rather than the rendered text (a subprocess ignores these).
            # Copies: the invocation owns its snapshot, so an impl cannot mutate
            # the engine's work order or the run-context view.
            inputs=dict(resolved_inputs),
            input_obj=input_obj,
            params=dict(ctx.params),
            # `prompt` above is the FULL body a renderer produced from these
            # channels, so compose_prompt must not prepend the run-wide blocks
            # again — carrying the parts is what tells it so.
            parts=parts,
        )
        # Executor selection (the engine is blind to all of this):
        #   1. --mock-agents mode ON + this node has a mock_agent -> MockExecutor
        #      (substitute, regardless of impl/runtime). Partial mocking: nodes
        #      without a mock_agent fall through to their normal executor.
        #   2. an in-process impl -> InProcessExecutor (direct Python call).
        #   3. else -> the runtime string selects a subprocess executor.
        # Same neutral invocation either way.
        executor = select_executor(
            name=name,
            agent=agent,
            ctx=ctx,
            ov=ov,
            registry=registry,
            impl=impl,
            runtime=runtime,
            resolved_inputs=resolved_inputs,
            tmpl=tmpl,
            log=log,
        )
        logger.debug(f"node {name}: executor={type(executor).__name__} prompt_chars={len(inv.prompt)} inputs={list(resolved_inputs)}")
        result = await executor.run(inv)
        log(
            f"node {name}: agent={agent} -> {result.control.get('status')} in {result.duration_s:.1f}s "
            f"(tokens={result.tokens} cost=${result.cost:.4f} completion={result.completion})"
        )
        # Hand the gate the control envelope plus a little telemetry, under keys
        # unlikely to collide with the agent's own result fields. `_result_obj`
        # is the VALIDATED typed object (a Pydantic model instance when a
        # PydanticSchema was used) so a gate can decide on typed fields directly,
        # e.g. ctx.result["_result_obj"].languages. None when no schema was given.
        return {
            **result.control,
            "_runtime": result.runtime,
            "_tokens": result.tokens,
            "_cost": result.cost,
            "_completion": result.completion,
            "_result_valid": result.result_valid,
            "_result_errors": list(result.result_errors),
            "_result_obj": result.result_obj,
            # The node's OWN resolved inputs (KEY: resolved-value), available to
            # gates via {KEY} templating. Local to this node instance — they WIN
            # over same-named global params in gate path resolution (most specific
            # wins) but do NOT flow into the shared run-context. This lets
            # gate_args reference the same value the node passed the agent
            # (e.g. {REPORT}) without repetition.
            "_inputs": dict(resolved_inputs),
        }

    return Node(
        name=name,
        run=run,
        gate=gate,
        gate_ref=gate_ref,
        gate_args=dict(gate_args or {}),
        depends_on=depends_on,
        parallel_group=parallel_group,
        criticality=criticality,
        max_cycles=max_cycles,
        result_schema=result_schema,
        input_schema=input_schema,
        duration=duration or "",
        agent=agent,
        exports=exports,
        export_ref=export_ref,
    )
