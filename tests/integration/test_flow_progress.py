"""Integration test: build_flow emits on_node_event and records node durations.

Runs a tiny 2-node flow with the --mock-agents mode (in-process MockExecutor, no
LLM, no subprocess) and asserts the node-lifecycle hook fires start/finish per
node and that each NodeOutcome carries a duration. Marked integration because it
crosses the Prefect task boundary.
"""

import pytest

pytestmark = pytest.mark.integration

# build_flow defaults to the local backend (no Prefect), so no bootstrap needed.
from agent_flow import agent_node, build_flow  # noqa: E402
from agent_flow.flow_types import NodeOutcome  # noqa: E402


def _stub(inv, ctx):  # a trivial mock_agent behaviour
    return {"status": "ok", "result": {"ran": inv.node}}


@pytest.mark.anyio
async def test_on_node_event_and_durations(tmp_path):
    from agent_flow.registry import FlowRegistry

    registry = FlowRegistry()
    registry.mock_agent("selftest-analyst")(_stub)

    events: list[tuple] = []
    nodes = [
        agent_node("analyze", agent="selftest-analyst"),
        agent_node("verify", agent="selftest-analyst", depends_on=("analyze",)),
    ]
    flow = build_flow(
        nodes,
        name="progress-probe",
        on_node_event=lambda n, p, s, a: events.append((n, p, s, a)),
    )
    result = await flow(run_dir=str(tmp_path), mock_agents=True)

    # Lifecycle: each node fires start (status None) then finish (a status).
    assert ("analyze", "start", None, "selftest-analyst") in events
    assert ("verify", "start", None, "selftest-analyst") in events
    finishes = [(n, s) for (n, p, s, _a) in events if p == "finish"]
    assert ("analyze", "ok") in [(n, s) for n, s in finishes]
    assert any(n == "verify" for n, _s in finishes)

    # start precedes finish for each node.
    order = [(n, p) for (n, p, _s, _a) in events]
    assert order.index(("analyze", "start")) < order.index(("analyze", "finish"))

    # Result carries NodeOutcome with a (non-negative) duration.
    assert set(result) == {"analyze", "verify"}
    assert all(isinstance(oc, NodeOutcome) for oc in result.values())
    assert all(oc.duration_s >= 0.0 for oc in result.values())
