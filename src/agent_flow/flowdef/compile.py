"""compile_flow — turn a FlowDef (data) into runtime Nodes (the compile target).

Resolves each NodeDef's NAME references against the FlowRegistry and produces the
internal `Node` list the engine runs. An `agent` node compiles to the standard
batteries agent-run (via agent_node); a `run_ref` node uses the registered custom
run. gate/export/result_schema names are threaded through so the engine resolves
them at run time (gates/exports) or the run uses them (schema).
"""

from __future__ import annotations

from agent_flow.engine import Node
from agent_flow.flowdef.models import FlowDef, NodeDef


def run_flow(flow_def: FlowDef, *, registry=None, run_dir: str = "", start_from: str = "", only: str = "", **params):
    """Compile and RUN a FlowDef in one call — the programmatic one-liner.

    Hides the plumbing: builds a default FlowRegistry (built-in gates) when none
    is given, compiles the FlowDef to nodes, builds the flow with the FlowDef's
    flow-wide settings (agent_dir/backend/shared_*/llm_concurrency), and runs it.
    Returns the {node: NodeOutcome} result. `params` are the run params (e.g.
    product_key=…, runtime=…). For the CLI (run/nodes list), use run_cli(flow_def).
    """
    from agent_flow.engine import build_flow

    if registry is None:
        from agent_flow.registry import FlowRegistry

        registry = FlowRegistry()
    nodes = compile_flow(flow_def, registry)
    pipeline = build_flow(
        nodes,
        name=flow_def.name,
        llm_concurrency=flow_def.llm_concurrency,
        shared_instructions=flow_def.shared_instructions,
        shared_context=flow_def.shared_context,
        agent_dir=flow_def.agent_dir,
        backend=flow_def.backend,
        registry=registry,
    )
    call = {"run_dir": run_dir, **params}
    if start_from:
        call["start_from"] = start_from
    if only:
        call["only"] = only
    return pipeline(**call)


def compile_flow(flow_def: FlowDef, registry) -> list[Node]:
    """Compile a FlowDef into runtime Nodes, resolving refs via `registry`.

    Validates that referenced gate / result_schema / run_ref names exist in the
    registry (fail fast, before any run). Returns nodes in declaration order;
    build_flow(plan_groups) orders them for execution.
    """
    _validate_refs(flow_def, registry)
    return [_compile_node(n, registry) for n in flow_def.nodes]


def _validate_refs(flow_def: FlowDef, registry) -> None:
    for n in flow_def.nodes:
        if n.gate and not registry.has_gate(n.gate):
            raise ValueError(f"node {n.name!r}: unknown gate {n.gate!r}")
        if n.result_schema and not registry.has_schema(n.result_schema):
            raise ValueError(f"node {n.name!r}: unknown result_schema {n.result_schema!r}")
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
    """A standard 'run one agent' node: delegate to the batteries builder.

    An `impl_ref` resolves to a registered in-process agent impl (the node then
    runs in-process, no subprocess); absent it, the node runs as a subprocess.
    """
    from agent_flow.batteries import agent_node

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
        exports=nd.exports,
        export_ref=nd.export_ref,
        model=nd.model,
        idle_timeout_s=nd.idle_timeout_s,
        agent_dir=nd.agent_dir,
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
