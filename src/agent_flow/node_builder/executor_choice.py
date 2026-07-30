"""Executor selection — which of the four executors runs this node.

Three-way choice, in priority order:
  1. --mock-agents mode ON and this agent has a registered mock -> MockExecutor
  2. an in-process impl -> InProcessExecutor
  3. otherwise the runtime string selects a subprocess executor

The engine is blind to all of it: the same neutral `AgentInvocation` is handed to
whichever executor is picked.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_flow.flow_types import RunContext
from agent_flow.runners import MockExecutor, get_executor
from agent_flow.runners.executor import AgentExecutor
from agent_flow.runners.inprocess import InProcessExecutor


def select_executor(
    *,
    name: str,
    agent: str,
    ctx: RunContext,
    ov: dict,
    registry: Any | None,
    impl: Callable[..., Any] | None,
    runtime: str,
    resolved_inputs: dict[str, str],
    tmpl: dict,
    log: Callable[[str], None],
) -> AgentExecutor:
    """Pick the executor for this node. See the module docstring for the order."""
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
    return executor
