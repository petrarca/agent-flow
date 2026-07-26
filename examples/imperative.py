"""Assessment pipeline, IMPERATIVE style — authored with agent_node.

A realistic shape with a parallel fan-out and analyst/verifier re-run loops:

    tech-stack -> tech-stack-verify
                       |
          +------------+------------+          (parallel)
    domain -> domain-verify   architecture -> architecture-verify
          +------------+------------+
                    summary

Every node runs one (simulated) agent; the agent .md files live in the shared
examples/.opencode/agent/ dir, so this runs with BOTH --runtime mock and opencode.
Analysts carry a `require_file` gate ("reported ok but wrote nothing -> retry");
verifiers carry a `rerun_on_signal` gate that jumps the flow back to their analyst.

The same pipeline is authored declaratively in examples/declarative.py.

Run:
    python -m examples.imperative run -p product_key=acme --runtime mock
    python -m examples.imperative run -p product_key=acme --runtime opencode
    python -m examples.imperative flow nodes
"""

from __future__ import annotations

from pathlib import Path

from agent_flow import FlowRegistry, agent_node, load_env
from agent_flow.engine import Node
from examples import mock_agents  # the flow-supplied mock behaviours (--mock-agents mode)

load_env()

# Registry carrying the mock_agent behaviours (resolved by agent name at run time).
# Only invoked under --mock-agents; harmless otherwise.
REGISTRY = FlowRegistry()
mock_agents.register(REGISTRY)

from pydantic import Field  # noqa: E402
from pydantic_settings import BaseSettings  # noqa: E402

# Shared example agent-definitions dir (holds .opencode/agent/*.md).
_EXAMPLES_DIR = str(Path(__file__).resolve().parent)


class AssessParams(BaseSettings):
    """Domain params (-p product_key=…), referenced in inputs via {product_key}."""

    product_key: str = Field(description="the product to (simulate) assessing")


def _analyst(name: str, agent: str, report: str, *, depends_on=(), parallel_group=None) -> Node:
    return agent_node(
        name=name,
        agent=agent,
        inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/" + report},
        depends_on=depends_on,
        parallel_group=parallel_group,
        gate_ref="require_file",
        gate_args={"relpath": report},
        registry=REGISTRY,
    )


def _verifier(name: str, agent: str, report: str, subject: str) -> Node:
    return agent_node(
        name=name,
        agent=agent,
        inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/" + report},
        depends_on=(subject,),
        criticality="degrade",
        gate_ref="rerun_on_signal",
        gate_args={"target": subject},
        registry=REGISTRY,
    )


def build_nodes() -> list[Node]:
    return [
        _analyst("tech-stack", "tech-stack-analyst", "tech-stack.md"),
        _verifier("tech-stack-verify", "tech-stack-verifier", "tech-stack.md", "tech-stack"),
        _analyst("domain", "domain-analyst", "domain.md", depends_on=("tech-stack-verify",), parallel_group="analysis"),
        _analyst("architecture", "architecture-analyst", "architecture.md", depends_on=("tech-stack-verify",), parallel_group="analysis"),
        _verifier("domain-verify", "domain-verifier", "domain.md", "domain"),
        _verifier("architecture-verify", "architecture-verifier", "architecture.md", "architecture"),
        _analyst("summary", "executive-summary", "summary.md", depends_on=("domain-verify", "architecture-verify")),
    ]


def main() -> None:
    from agent_flow.cli import run_cli

    run_cli(build_nodes, name="assessment (imperative)", default_agent_dir=_EXAMPLES_DIR, params_model=AssessParams, registry=REGISTRY)


if __name__ == "__main__":
    main()
