"""Unit tests for the duration vocabulary — portable INTENT in the flow, seconds in the run.

A node declares `duration="long"` (meaningful on any machine); the run config maps
that name to concrete seconds via `durations:`. These tests pin the three things
that make the split trustworthy:

  1. the name actually reaches the invocation as seconds,
  2. an unknown name fails LOUDLY at build time, not silently at run time,
  3. a node that declares nothing still gets the run-wide idle timeout.
"""

import tempfile
from pathlib import Path

import anyio
import pytest

from agent_flow.core import DEFAULT_IDLE_TIMEOUT_S
from agent_flow.engine import build_flow, interpret
from agent_flow.node_builder import agent_node
from agent_flow.utils import DEFAULT_DURATIONS, duration_table, resolve_duration


def _capture_idle(monkeypatch, node, *, params=None, durations=None):
    """Run one node with a stubbed executor; return the invocation's idle_timeout_s."""
    from agent_flow.core.agent_runtime import AgentResult

    captured = {}

    class _FakeExecutor:
        name = "fake"

        async def run(self, inv):
            captured["idle"] = inv.idle_timeout_s
            return AgentResult(agent=inv.agent, exit_code=0, duration_s=0.0, control={"status": "ok"}, completion="completed")

    monkeypatch.setattr("agent_flow.node_builder.get_executor", lambda _runtime, **_kw: _FakeExecutor())
    anyio.run(
        lambda: interpret(
            node,
            run_dir=Path(tempfile.gettempdir()),
            params=params or {},
            on_error=lambda n, e: "degraded",
            durations=durations or {},
        )
    )
    return captured["idle"]


# --- the vocabulary itself --------------------------------------------------


def test_normal_matches_the_runner_default():
    """`duration="normal"` and the run-wide default are the same budget, reached by
    name vs by number. Both live in `const` and `DEFAULT_DURATIONS["normal"]`
    references `DEFAULT_IDLE_TIMEOUT_S` directly, so drift is structurally
    impossible — this pins that wiring against a future refactor that re-splits
    them (e.g. hard-coding the literal back into the map)."""
    from agent_flow.const import DEFAULT_DURATIONS as CONST_DURATIONS
    from agent_flow.const import DEFAULT_IDLE_TIMEOUT_S as CONST_IDLE

    assert DEFAULT_DURATIONS["normal"] == DEFAULT_IDLE_TIMEOUT_S
    assert CONST_DURATIONS["normal"] is CONST_IDLE  # same object, not just equal


def test_run_durations_overlay_rather_than_replace():
    """Retuning one name must not empty the vocabulary — the failure mode of a
    plain dict assignment."""
    table = duration_table({"long": 900})
    assert table["long"] == 900  # retuned
    assert table["short"] == DEFAULT_DURATIONS["short"]  # survived
    assert table["normal"] == DEFAULT_DURATIONS["normal"]


def test_a_run_may_add_its_own_names():
    assert duration_table({"epic": 3600})["epic"] == 3600


def test_unknown_name_raises_and_names_the_alternatives():
    with pytest.raises(ValueError) as exc:
        resolve_duration("analyst", "lng", {"long": 900})
    msg = str(exc.value)
    assert "lng" in msg and "analyst" in msg
    assert "long" in msg and "short" in msg  # the message teaches the vocabulary


# --- resolution reaching the invocation -------------------------------------


def test_declared_duration_becomes_seconds_on_the_invocation(monkeypatch):
    node = agent_node("n", "a", duration="long")
    assert _capture_idle(monkeypatch, node, durations={"long": 900}) == 900


def test_shipped_vocabulary_works_with_zero_configuration(monkeypatch):
    """A flow must run on a machine whose run config defines no durations at all."""
    node = agent_node("n", "a", duration="long")
    assert _capture_idle(monkeypatch, node, durations={}) == DEFAULT_DURATIONS["long"]


def test_no_duration_falls_back_to_the_run_wide_idle_timeout(monkeypatch):
    node = agent_node("n", "a")
    assert _capture_idle(monkeypatch, node, params={"idle_timeout_s": "45"}) == 45


def test_no_duration_and_no_run_wide_value_uses_the_library_default(monkeypatch):
    node = agent_node("n", "a")
    assert _capture_idle(monkeypatch, node) == DEFAULT_IDLE_TIMEOUT_S


def test_declared_duration_beats_the_run_wide_idle_timeout(monkeypatch):
    """Specificity within a tier: the node's own declaration wins over the
    run-wide fallback."""
    node = agent_node("n", "a", duration="short")
    idle = _capture_idle(monkeypatch, node, params={"idle_timeout_s": "45"}, durations={"short": 30})
    assert idle == 30


# --- build-time validation --------------------------------------------------


def test_build_flow_rejects_an_unknown_duration():
    """The whole graph is in hand at build time, so a typo must fail HERE — not
    when that node's turn comes, halfway through a paid run."""
    with pytest.raises(ValueError, match="unknown duration"):
        build_flow([agent_node("n", "a", duration="lnog")], durations={"long": 900})


def test_build_flow_accepts_a_name_the_run_config_adds():
    flow = build_flow([agent_node("n", "a", duration="epic")], durations={"epic": 3600})
    assert callable(flow)


def test_build_flow_validates_every_node_not_just_the_first():
    with pytest.raises(ValueError, match="'later'"):
        build_flow([agent_node("first", "a", duration="long"), agent_node("later", "a", duration="nope")])


# --- the declarative (FlowDef) path -----------------------------------------


def test_flowdef_carries_duration_through_compile():
    """Tier-3: the portable name survives NodeDef -> Node, so build_flow can check
    it and the node can resolve it."""
    from agent_flow.flowdef import FlowDef, NodeDef, compile_flow
    from agent_flow.registry import FlowRegistry

    fd = FlowDef(name="f", nodes=[NodeDef(name="n", agent="a", duration="long")])
    nodes = compile_flow(fd, FlowRegistry())
    assert nodes[0].duration == "long"


def test_nodedef_no_longer_accepts_raw_seconds():
    """idle_timeout_s was an environment fact on portable data; it is gone."""
    from pydantic import ValidationError

    from agent_flow.flowdef import NodeDef

    with pytest.raises(ValidationError):
        NodeDef(name="n", agent="a", idle_timeout_s=600)


# --- BOTH entry points ------------------------------------------------------
#
# A run-config value reaches nodes through build_flow, and there are two callers
# of build_flow: run_flow (programmatic) and the CLI. Wiring only one is how
# `run_context` and `run_instructions` each came to be silently dropped under
# run_cli. Assert both, so `durations` cannot drift the same way.


@pytest.fixture
def flow_with_duration():
    from agent_flow.flowdef import FlowDef, NodeDef
    from agent_flow.registry import FlowRegistry

    seen: dict[str, int] = {}
    registry = FlowRegistry()

    @registry.mock_agent("a")
    def _capture(inv, ctx):  # noqa: ARG001
        seen["idle"] = inv.idle_timeout_s
        return {"status": "ok"}

    flow = FlowDef(name="t", nodes=[NodeDef(name="n", agent="a", duration="long")])
    return flow, registry, seen


def test_programmatic_run_flow_delivers_durations(flow_with_duration, tmp_path):
    from agent_flow import run_flow

    flow, registry, seen = flow_with_duration
    run_flow(flow, registry=registry, run_dir=str(tmp_path), mock_agents=True, run_config={"durations": {"long": 900}})
    assert seen["idle"] == 900


def test_cli_path_delivers_durations(flow_with_duration, tmp_path):
    """The same FlowDef, run the way `run_cli` runs it."""
    from agent_flow.cli.commands.run import _build_and_run
    from agent_flow.cli.console import get_console
    from agent_flow.flowdef import compile_flow
    from agent_flow.run_config import build_run_config

    flow, registry, seen = flow_with_duration
    cfg = build_run_config(run_dir=str(tmp_path), mock_agents=True, durations={"long": 900})
    _build_and_run(
        compile_flow(flow, registry),
        {},
        cfg,
        get_console(),
        name="t",
        llm_tag="llm",
        on_event_factory=None,
        on_node_event=None,
        render_results=False,
        registry=registry,
    )
    assert seen["idle"] == 900, "run_cli must not drop the run config's `durations:` map"
