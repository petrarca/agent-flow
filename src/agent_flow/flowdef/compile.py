"""compile_flow — turn a FlowDef (data) into runtime Nodes (the compile target).

Resolves each NodeDef's NAME references against the FlowRegistry and produces the
internal `Node` list the engine runs. An `agent` node compiles to the standard
standard agent-run (via agent_node); a `run_ref` node uses the registered custom
run. gate/export/result_schema names are threaded through so the engine resolves
them at run time (gates/exports) or the run uses them (schema).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import anyio

from agent_flow.flow_types import Node
from agent_flow.flowdef.models import FlowDef, NodeDef

if TYPE_CHECKING:
    from agent_flow.run_config import RunConfig


def _build_pipeline_and_call(
    flow_def: FlowDef,
    registry,
    run_dir: str,
    start_from: str,
    only: str,
    params: dict,
    run_config: dict[str, Any] | RunConfig | None = None,
):
    """Shared plumbing for (a)run_flow: build the flow callable + assemble the
    call kwargs. Returns (pipeline, call_kwargs). Both entry points differ only in
    how they invoke the (async) pipeline callable.

    `run_config` (a dict or a RunConfig) carries the run-side settings that used
    to live on the FlowDef (agent_dir/backend/llm_concurrency) plus durations /
    nodes / options. It is resolved through the SAME RunConfig source stack as the
    CLI, so env / .env still apply below it."""
    from agent_flow.engine import build_flow
    from agent_flow.run_config import RunConfig, build_run_config, validate_params

    if registry is None:
        from agent_flow.registry import FlowRegistry

        registry = FlowRegistry()
    # A RunConfig instance is ALREADY resolved: it is a BaseSettings, so env /
    # .env were applied when it was constructed and the caller's explicit values
    # already won. Honour it verbatim — re-resolving it as a base layer would
    # apply env a second time AND demote the caller's values below it, which is
    # wrong for a consumer who did their own resolution (their own CLI, or other
    # tooling). A plain dict means "my defaults", so it stays the lowest layer.
    cfg = run_config if isinstance(run_config, RunConfig) else build_run_config(base=run_config)
    nodes = compile_flow(flow_def, registry)
    # The flow's declared SIGNATURE applies here too — not only under run_cli —
    # so a missing/invalid param fails the same way on both entry points. A
    # ValidationError propagates (a library raises; the CLI catches and exits 2).
    #
    # OVERLAY, never replace: this path's `params` carries the FRAMEWORK keys too
    # (runtime / mock_agents / model / idle_timeout_s — the reserved names an
    # agent-node reads back out of the bag). A domain model ignores unknown
    # fields, so validating the whole bag and taking the result would silently
    # DROP them. Validated domain values win; everything else passes through.
    if flow_def.params_schema:
        model = registry.get_params_model(flow_def.params_schema)
        params = {**params, **validate_params(model, params)}
    pipeline = build_flow(
        nodes,
        name=flow_def.name,
        llm_concurrency=cfg.llm_concurrency,
        # STANDING brief (flow) + this run's ADDITION (run_config instructions) as
        # SEPARATE channels — neither dropped (the 0.3.0 bug this stage fixes).
        run_instructions=flow_def.run_instructions,
        run_additional_instructions=cfg.resolved_instructions(),
        run_context=flow_def.run_context,
        agent_dir=cfg.agent_dir,
        backend=cfg.backend,
        durations=cfg.durations,
        node_overrides=cfg.node_overrides(),
        options=cfg.options,
        registry=registry,
    )
    # Run-wide model / idle_timeout_s ride `params` (the node builder reads them
    # from ctx.params, per node) — seeded by the SAME helper the CLI uses, so the
    # two entry points cannot drift (they did once: run_config={"model": ...} was
    # silently dropped on this path).
    cfg.apply_run_wide_params(params)
    # An explicit run_dir= arg wins over run_config's run_dir (a convenience for
    # the common "same flow, different output dir" call); else fall to the config.
    call = {"run_dir": run_dir or cfg.run_dir, **params}
    if start_from:
        call["start_from"] = start_from
    if only:
        call["only"] = only
    return pipeline, call


async def arun_flow(
    flow_def: FlowDef,
    *,
    registry=None,
    run_dir: str = "",
    start_from: str = "",
    only: str = "",
    run_config: dict[str, Any] | RunConfig | None = None,
    **params,
):
    """Compile and RUN a FlowDef in one call — the async programmatic one-liner.

    The native async entry: `await arun_flow(...)` composes on a consumer's event
    loop (a FastAPI handler, a notebook) with no bridging. Same behaviour as
    `run_flow`, minus the `anyio.run` wrapper. Returns the {node: NodeOutcome}.

    `run_config` is the run-side configuration: agent_dir, backend,
    llm_concurrency, durations, nodes (per-node overrides), options, run_dir. It
    is the single home for everything that is NOT portable pipeline data — an
    explicit keyword, NOT a param, so `**params` cannot silently swallow it.

    Two accepted shapes, and the difference matters:
      - a **dict** — "my defaults". Resolved as the LOWEST layer, so env / .env
        still override it.
      - a **RunConfig** — "already resolved". Honoured verbatim. A RunConfig is a
        BaseSettings, so env was applied when it was built and the caller's
        explicit values already won; re-resolving would apply env twice and demote
        them. This is the path for a consumer who did their own resolution — their
        own CLI, or integration into other tooling:

            cfg = build_run_config(config_file=args.config, model=args.model)
            run_flow(flow, run_config=cfg, **params)

    `params` are the DOMAIN run params (product_key=…, runtime=…).
    """
    pipeline, call = _build_pipeline_and_call(flow_def, registry, run_dir, start_from, only, params, run_config)
    return await pipeline(**call)


def run_flow(
    flow_def: FlowDef,
    *,
    registry=None,
    run_dir: str = "",
    start_from: str = "",
    only: str = "",
    run_config: dict[str, Any] | RunConfig | None = None,
    **params,
):
    """Compile and RUN a FlowDef in one call — the sync programmatic one-liner.

    A thin `anyio.run` wrapper over `arun_flow`, keeping the long-standing
    blocking signature for consumers not on an event loop (scripts, the CLI).
    `run_config` carries the run-side settings — a dict is treated as defaults, a
    RunConfig is honoured verbatim; see arun_flow. `params` are the DOMAIN run
    params.
    """
    return anyio.run(
        lambda: arun_flow(
            flow_def,
            registry=registry,
            run_dir=run_dir,
            start_from=start_from,
            only=only,
            run_config=run_config,
            **params,
        )
    )


def compile_flow(flow_def: FlowDef, registry) -> list[Node]:
    """Compile a FlowDef into runtime Nodes, resolving refs via `registry`.

    Validates that referenced gate / result_schema / run_ref names exist in the
    registry (fail fast, before any run). Returns nodes in declaration order;
    build_flow(plan_groups) orders them for execution.
    """
    _validate_refs(flow_def, registry)
    return [_compile_node(n, registry) for n in flow_def.nodes]


def _validate_refs(flow_def: FlowDef, registry) -> None:
    # Flow-level refs first: a typo'd params_schema must fail with the same
    # fail-fast guarantee as a node's gate/schema ref, before anything runs.
    if flow_def.params_schema and not registry.has_params_model(flow_def.params_schema):
        raise ValueError(f"flow {flow_def.name!r}: unknown params_schema {flow_def.params_schema!r}")
    for n in flow_def.nodes:
        if n.gate and not registry.has_gate(n.gate):
            raise ValueError(f"node {n.name!r}: unknown gate {n.gate!r}")
        if n.result_schema and not registry.has_schema(n.result_schema):
            raise ValueError(f"node {n.name!r}: unknown result_schema {n.result_schema!r}")
        if n.input_schema and not registry.has_schema(n.input_schema):
            raise ValueError(f"node {n.name!r}: unknown input_schema {n.input_schema!r}")
        if n.run_ref:
            registry.get_run(n.run_ref)  # raises if unknown
        if n.export_ref:
            registry.get_export(n.export_ref)  # raises if unknown
        if n.impl_ref and not registry.has_agent_impl(n.impl_ref):
            raise ValueError(f"node {n.name!r}: unknown agent impl {n.impl_ref!r}")


def _compile_node(nd: NodeDef, registry) -> Node:
    """One NodeDef -> one runtime Node."""
    schema = registry.get_schema(nd.result_schema) if nd.result_schema else None
    if nd.agent:
        return _compile_agent_node(nd, registry, schema)
    return _compile_custom_node(nd, registry, schema)


def _compile_agent_node(nd: NodeDef, registry, schema) -> Node:
    """A standard 'run one agent' node: delegate to the node builder.

    An `impl_ref` resolves to a registered in-process agent impl (the node then
    runs in-process, no subprocess); absent it, the node runs as a subprocess.

    Precondition: `nd.agent` is set — `_compile_node` dispatches here only when it
    is (a NodeDef without an agent compiles to a custom-run node instead).
    """
    from agent_flow.node_builder import agent_node

    assert nd.agent, "internal: _compile_agent_node requires NodeDef.agent"
    impl = registry.get_agent_impl(nd.impl_ref) if nd.impl_ref else None
    return agent_node(
        name=nd.name,
        agent=nd.agent,
        inputs=nd.inputs or None,
        instructions=nd.instructions,
        context=tuple(nd.context),
        depends_on=tuple(nd.depends_on),
        parallel_group=nd.parallel_group,
        criticality=nd.criticality,
        max_cycles=nd.max_cycles,
        gate_ref=nd.gate,
        gate_args=nd.gate_args,
        result_schema=schema,
        input_schema=registry.get_schema(nd.input_schema) if nd.input_schema else None,
        exports=nd.exports,
        export_ref=nd.export_ref,
        duration=nd.duration,
        impl=impl,
    )


def _compile_custom_node(nd: NodeDef, registry, schema) -> Node:
    """A custom-run node: `run` is a registered function referenced by run_ref."""
    run = registry.get_run(nd.run_ref)
    return Node(
        name=nd.name,
        run=run,
        depends_on=tuple(nd.depends_on),
        parallel_group=nd.parallel_group,
        criticality=nd.criticality,
        max_cycles=nd.max_cycles,
        gate_ref=nd.gate,
        gate_args=dict(nd.gate_args),
        result_schema=schema,
        exports=nd.exports,
        export_ref=nd.export_ref,
    )
