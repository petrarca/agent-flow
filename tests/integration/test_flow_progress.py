"""Integration test: build_flow emits on_node_event and records node durations.

Runs a tiny 2-node flow on the MOCK runtime (real Prefect task boundary, no LLM)
and asserts the node-lifecycle hook fires start/finish per node and that each
NodeOutcome carries a duration. Marked integration because it crosses the
Prefect task + subprocess boundary.
"""

import pytest

pytestmark = pytest.mark.integration

# build_flow defaults to the local backend (no Prefect), so no bootstrap needed.
from pathlib import Path  # noqa: E402

from agent_flow import agent_node, build_flow  # noqa: E402
from agent_flow.engine import NodeOutcome  # noqa: E402

# The mock runtime spawns the packaged _mock_agent.py and does NOT validate the
# agent-dir layout, so this test owns a self-contained fixture agent dir (it does
# not depend on examples/ content). The agent name is cosmetic under mock.
_FIXTURE_DIR = str(Path(__file__).resolve().parents[1] / "fixtures" / "opencode")


def test_on_node_event_and_durations(tmp_path):
    events: list[tuple] = []
    nodes = [
        agent_node("analyze", agent="selftest-analyst"),
        agent_node("verify", agent="selftest-analyst", depends_on=("analyze",)),
    ]
    flow = build_flow(
        nodes,
        name="progress-probe",
        agent_dir=_FIXTURE_DIR,
        on_node_event=lambda n, p, s, a: events.append((n, p, s, a)),
    )
    result = flow(run_dir=str(tmp_path), runtime="mock")

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
