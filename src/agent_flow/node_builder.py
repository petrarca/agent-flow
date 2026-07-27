"""Node builder — the one-call helper for the common case: a node that runs ONE agent.

Layer 3's `Node` takes a `run` callable, which is maximally flexible but means a
consumer hand-writes prompt-building, control-path derivation, and the executor
call for every node. The overwhelmingly common shape is simply: "run one agent,
hand it a work order of KEY: value inputs, get the result." `agent_node(...)`
builds exactly that node in one call.

It is a CONVENIENCE, not a new layer: it returns a plain `Node`, so it composes
with hand-written `run` callables in the same graph. Domain-neutral by design —
there is NO notion of "analyst"/"verifier" here. A verifier is just another
`agent_node` that `depends_on` its subject and carries a `rerun_on_signal(...)`
gate; the engine's bounded `GoTo` drives the re-run. Any node can route flow to
any upstream node — the library imposes no adjacency.

This module is the one place allowed to depend on BOTH the engine (Node) and the
runner/executor seam — keeping `engine.py` itself decoupled from the runtime.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_flow.core import DEFAULT_IDLE_TIMEOUT_S
from agent_flow.engine import Criticality, Node, RunContext
from agent_flow.gates import Gate
from agent_flow.runners import AgentInvocation, MockExecutor, get_executor, probe_agent_dir
from agent_flow.runners.base import PromptParts, render_prompt
from agent_flow.runners.executor import AgentExecutor
from agent_flow.runners.inprocess import InProcessExecutor
from agent_flow.utils import resolve_duration, resolve_template


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


def _validate_inputs(node: str, input_schema: object, resolved: dict[str, str]) -> object | None:
    """Validate a node's RESOLVED work order against its `input_schema`.

    The mirror of the result-schema check, on the way IN. Runs after templating,
    so an unresolved `{param}`/`{export}` surfaces as a real schema error here —
    before an agent is spawned — instead of reaching the agent as the literal
    text "{mode}". Returns the typed instance (a pydantic model) for an in-process
    impl to use, or None when no schema is declared (or a plain dict JSON-schema
    was used, which validates but yields no new object).

    Raises ValueError on invalid input; `interpret` maps that through the node's
    criticality like any other node failure (blocking halts, degrade degrades).
    """
    if input_schema is None:
        return None
    from agent_flow.core.schema import coerce_schema

    schema = coerce_schema(input_schema)
    if schema is None:
        return None
    outcome = schema.validate(resolved)
    if not outcome.valid:
        raise ValueError(f"node {node!r}: inputs do not match input_schema: {'; '.join(outcome.errors)}")
    return outcome.obj


def resolve_work_order(inputs: dict[str, str], params: dict[str, Any]) -> dict[str, str]:
    """Resolve `{param}` templates in every input value; return the structured dict.

    This is the single place where input values are resolved. Both the prompt
    representation (KEY: value lines) and the structured MockAgentContext.input()
    dict are derived from it.
    """
    return {key: resolve_template(val, params) for key, val in inputs.items()}


# A work-order RENDERER turns the resolved `{KEY: value}` work order into the
# prompt text an agent sees. It is a seam: the default is XML, and a consumer may
# pass any callable (per node, or flow-wide) to control the shape entirely.
WorkOrderRenderer = Callable[[dict[str, str]], str]


def render_work_order_xml(resolved: dict[str, str]) -> str:
    """Render the work order as XML-ish tags — the DEFAULT.

        <PRODUCT_KEY>acme</PRODUCT_KEY>
        <REPORT>/run/report.md</REPORT>

    Why this and not `KEY: value`: a closing tag DELIMITS the value, so a
    multi-line or structured value is unambiguous (a line-oriented work order has
    no continuation marker, so its second line is indistinguishable from the next
    key). Tags are also the shape Anthropic recommends for Claude prompts, and an
    agent resolves `<REPORT>` without being told anything about the format — the
    instructions in an agent's .md refer to the KEY name, which is unchanged.

    A multi-line value is placed on its own lines so both the value and the
    surrounding tags stay readable. Values are NOT XML-escaped: this is prompt
    text for a model, not a document for a parser, and escaping would only make
    it harder to read.
    """
    parts: list[str] = []
    for key, val in resolved.items():
        if "\n" in val:
            parts.append(f"<{key}>\n{val}\n</{key}>")
        else:
            parts.append(f"<{key}>{val}</{key}>")
    return "\n".join(parts)


def render_work_order_lines(resolved: dict[str, str]) -> str:
    """Render the work order as `KEY: value` lines — the pre-0.3 shape.

        PRODUCT_KEY: acme
        REPORT: /run/report.md

    Kept as a shipped renderer so a pipeline tuned on this shape can opt back in
    (`build_flow(work_order_renderer=render_work_order_lines)`). Note a value
    containing a newline is ambiguous here — that is precisely what the XML
    default fixes.
    """
    return "\n".join(f"{key}: {val}" for key, val in resolved.items())


#: Used when neither the node nor the flow supplies a renderer.
DEFAULT_WORK_ORDER_RENDERER: WorkOrderRenderer = render_work_order_xml


def build_work_order(inputs: dict[str, str], params: dict[str, Any], *, render: WorkOrderRenderer | None = None) -> str:
    """Resolve `{param}` templates in `inputs` and render the work-order prompt.

    Each value may reference run params via `{name}` (e.g. "{product_key}"); the
    library exposes nothing implicitly — the consumer decides the keys. The
    completion protocol (CONTROL_FILE + control JSON shape) is injected separately
    by the executor, so it is NOT part of these inputs.

    `render` selects the shape (default: `render_work_order_xml`).
    """
    resolved = resolve_work_order(inputs, params)
    return (render or DEFAULT_WORK_ORDER_RENDERER)(resolved)


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
    registry: Any | None = None,
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
        registry: optional FlowRegistry carrying `mock_agent` behaviours (and/or
            gates, exports, schemas). When `--mock-agents` mode is ON
            (mock_agents=True param) and the registry has a registered
            `mock_agent` for this node's `agent` name, the node runs that
            behaviour via MockExecutor — no subprocess, no tokens. Mocks are
            keyed by AGENT name (not by node), so one registration covers every
            node that runs the same agent. Absent registry or no matching
            `mock_agent`, the node runs normally (partial mock). Mock is NOT a
            runtime. See runners/mock_exec.py and docs design/mock-agent.md.

    Returns a plain `Node`, so it mixes freely with hand-written `run` nodes.
    """
    inputs = inputs or {}

    async def run(ctx: RunContext) -> dict:
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

        runtime = ctx.params.get("runtime", "opencode")
        # Per-setting precedence (most specific first): the run config's per-node
        # entry (this run) > the agent_node() arg (the flow's standing declaration)
        # > the run-wide value > the RUNNER PROBE > (empty -> preflight error).
        # `ov` is that per-node entry. agent_dir is templated; the probe is the
        # comfort fallback so the common case needs no explicit agent_dir at all.
        explicit_agent_dir = ov.get("agent_dir") or agent_dir or ctx.agent_dir or ""
        eff_agent_dir = resolve_template(explicit_agent_dir, tmpl) if explicit_agent_dir else (probe_agent_dir(runtime) or "")
        # model: empty ("") means "no model" — the runner omits --model and the
        # runtime resolves it (never a hardcoded one).
        eff_model = ov.get("model") or model or ctx.params.get("model") or ""
        # Liveness budget resolution, most specific first:
        #   1. the per-node run-config idle_timeout_s (a raw second-count override),
        #   2. the per-node duration NAME (run config's, then the flow-declared),
        #   3. the run-wide idle timeout (rides `params`, read per node at run time),
        #   4. the library default.
        # build_flow already rejected an unknown duration name; the resolve here is
        # also the sole guard when a Tier-2 flow calls interpret() without build_flow.
        run_wide_idle = int(ctx.params.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S)
        eff_duration = ov.get("duration") or duration
        if ov.get("idle_timeout_s") is not None:
            eff_idle = int(ov["idle_timeout_s"])
        elif eff_duration:
            eff_idle = resolve_duration(name, eff_duration, ctx.durations)
        else:
            eff_idle = run_wide_idle
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
        mock_on = bool(ctx.params.get("mock_agents"))
        # Resolve the mock_agent behaviour by AGENT name (not node name) from the
        # registry. Mocks are per-agent: one registration covers every node that
        # runs the same agent. Partial mock: no matching registration -> normal path.
        # Registry namespacing: mock_agent and agent_impl live in SEPARATE registry
        # dicts — a name "classify" as a mock_agent never collides with "classify"
        # as an agent_impl, gate, or export. When mock mode is on and a mock_agent
        # exists for this agent, it WINS over an in-process impl (the mock_agents
        # mode is designed to override everything, including impl nodes).
        _mock_behaviour = registry.get_mock_agent(agent) if (mock_on and registry is not None and registry.has_mock_agent(agent)) else None
        if _mock_behaviour is not None:
            _behaviour_name = getattr(_mock_behaviour, "__name__", repr(_mock_behaviour))
            log(f"node {name}: --mock-agents ON -> MockExecutor (agent={agent} behaviour={_behaviour_name})")
            # Annotated at the seam type: the three branches below pick different
            # concrete executors, all of which satisfy the AgentExecutor contract.
            executor: AgentExecutor = MockExecutor(_mock_behaviour, work_order=resolved_inputs, tmpl=tmpl)
        elif impl is not None:
            # In-process runs are labeled "inproc" (their canonical runtime), NOT
            # the `runtime` string — that names a SUBPROCESS runtime and does not
            # describe an in-process call.
            executor = InProcessExecutor(impl)
        else:
            # Runtime-specific options: run-wide (ctx.options) with this node's
            # own entry merged OVER it (per-node wins, key by key). An open bag
            # the runtime interprets — e.g. serve_url for a remote runtime.
            eff_options = {**ctx.options, **(ov.get("options") or {})}
            executor = get_executor(runtime, options=eff_options)
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
