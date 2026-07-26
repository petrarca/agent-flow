"""Assessment pipeline, DECLARATIVE style — the SAME graph as a FlowDef.

Identical pipeline to examples/imperative.py, but authored as pure DATA: a
FlowDef of NodeDefs, written out flat so the definition IS the pipeline. Agents
and gates are referenced BY NAME (the definition holds no callables); the whole
FlowDef is serializable (a pydantic model). It also shows how to HOOK YOUR OWN
LOGIC: a custom gate + an observing hook registered on the FlowRegistry and
referenced by name. run_cli takes the FlowDef directly and compiles it.

    tech-stack -> tech-stack-verify -> ( domain(+verify) | architecture(+verify) ) -> summary

Run:
    python -m examples.declarative run -p product_key=acme --runtime mock
    python -m examples.declarative run -p product_key=acme --runtime opencode
    python -m examples.declarative flow nodes
"""

from __future__ import annotations

from pathlib import Path

from agent_flow import FlowDef, FlowRegistry, NodeDef, load_env
from agent_flow.gates import Continue, GateContext, Stop
from examples import mock_agents  # the flow-supplied mock behaviours (--mock-agents mode)

load_env()

from pydantic import Field  # noqa: E402
from pydantic_settings import BaseSettings  # noqa: E402


class AssessParams(BaseSettings):
    product_key: str = Field(description="the product to (simulate) assessing")


# --- Hooking your own logic -------------------------------------------------
# A node references logic BY NAME (data); the implementations live here, on a
# FlowRegistry. Two kinds are shown:
#   1) a custom DECIDING gate (returns a Directive — steers the flow), and
#   2) an OBSERVING hook (telemetry — never steers the flow).
# Built-in gates (require_file / rerun_on_signal) are already seeded, so only the
# custom ones need registering.

REGISTRY = FlowRegistry()

# Register the mock_agent behaviours by name (analyst/verifier per agent). Only
# invoked under --mock-agents; harmless otherwise. Resolved onto each node at
# compile time by agent name.
mock_agents.register(REGISTRY)


@REGISTRY.gate("tech_stack_usable")
def tech_stack_usable(ctx: GateContext):
    """Custom gate for the tech-stack node: stop the run if the stack is unusable.

    A plain gate is just `(GateContext) -> Directive` — inspect what the agent
    produced and return a directive. Here: Stop the whole pipeline when the
    report signals it could not determine a stack, else Continue. A node
    references it by name: gate="tech_stack_usable".
    """
    status = (ctx.result or {}).get("status") if isinstance(ctx.result, dict) else None
    if status == "error":
        return Stop(reason="tech-stack could not be determined — nothing to assess")
    return Continue()


@REGISTRY.on("after_node")
def _log_cost(node, outcome) -> None:
    """Observing hook: per-node telemetry. Fires after EVERY node; steers nothing.

    Demonstrates a cross-cutting observer registered once and applied to all
    nodes (a node-scoped variant is `@REGISTRY.on("after_node", node="…")`).
    Prints a compact per-node line so you can see it during a run.
    """
    print(f"    [hook] {node.name}: {outcome.status} ({outcome.duration_s:.1f}s)")


# The whole pipeline as flat DATA. Each node: which agent, its work-order inputs
# ({product_key}/{run_dir} templated at run time), its DAG wiring, and a gate by
# NAME (require_file / rerun_on_signal are built-ins, so no registration needed).
# analysts gate on require_file (retry if the report wasn't written); verifiers
# gate on rerun_on_signal (jump back to their analyst when they flag a re-run).
FLOW = FlowDef(
    name="assessment (declarative)",
    agent_dir=str(Path(__file__).resolve().parent),  # shared examples/.opencode/agent/
    nodes=[
        NodeDef(
            name="tech-stack",
            agent="tech-stack-analyst",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
            # A CUSTOM gate (registered above), referenced by name — decides
            # whether the run can proceed. The built-in require_file gate is used
            # by the other analysts below.
            gate="tech_stack_usable",
        ),
        NodeDef(
            name="tech-stack-verify",
            agent="tech-stack-verifier",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
            depends_on=["tech-stack"],
            criticality="degrade",
            gate="rerun_on_signal",
            gate_args={"target": "tech-stack"},
        ),
        NodeDef(
            name="domain",
            agent="domain-analyst",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/domain.md"},
            depends_on=["tech-stack-verify"],
            parallel_group="analysis",
            gate="require_file",
            # {REPORT} resolves from the node's own inputs above — no need to
            # repeat the path. Node-local inputs are available to gates and win
            # over same-named global params (but never flow into the shared run
            # context).
            gate_args={"path": "{REPORT}"},
        ),
        NodeDef(
            name="architecture",
            agent="architecture-analyst",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/architecture.md"},
            depends_on=["tech-stack-verify"],
            parallel_group="analysis",
            gate="require_file",
            gate_args={"path": "{REPORT}"},  # resolves from this node's inputs
        ),
        NodeDef(
            name="domain-verify",
            agent="domain-verifier",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/domain.md"},
            depends_on=["domain"],
            criticality="degrade",
            gate="rerun_on_signal",
            gate_args={"target": "domain"},
        ),
        NodeDef(
            name="architecture-verify",
            agent="architecture-verifier",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/architecture.md"},
            depends_on=["architecture"],
            criticality="degrade",
            gate="rerun_on_signal",
            gate_args={"target": "architecture"},
        ),
        NodeDef(
            name="summary",
            agent="executive-summary",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/summary.md"},
            depends_on=["domain-verify", "architecture-verify"],
        ),
    ],
)


def main() -> None:
    from agent_flow.cli import run_cli

    # agent_dir comes from the FlowDef (FLOW.agent_dir); REGISTRY carries the
    # custom gate + observing hook (plus the seeded built-in gates).
    run_cli(FLOW, registry=REGISTRY, params_model=AssessParams)


if __name__ == "__main__":
    main()
