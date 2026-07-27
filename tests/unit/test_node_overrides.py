"""Per-node run config — RunConfig.nodes / node_overrides.

Non-portable per-node settings (model, agent_dir, idle timeout, an extra
instruction) moved OFF the portable NodeDef and INTO the run config's `nodes:`
section. These tests pin:

  1. each setting reaches the invocation from a per-node entry,
  2. precedence: per-node run config > agent_node() arg > run-wide > default,
  3. an unknown node key is a hard error at build time (was silently ignored),
  4. NodeDef no longer accepts model / agent_dir,
  5. both entry points (programmatic + CLI) deliver the overrides.
"""

import tempfile
from pathlib import Path

import anyio
import pytest

from agent_flow.engine import build_flow, interpret
from agent_flow.node_builder import agent_node


def _capture(spy, node, *, params=None, node_overrides=None, durations=None):
    """Run one node through the shared executor spy; return the captured invocation."""
    anyio.run(
        lambda: interpret(
            node,
            run_dir=Path(tempfile.gettempdir()),
            params=params or {},
            on_error=lambda n, e: "degraded",
            node_overrides=node_overrides or {},
            durations=durations or {},
        )
    )
    return spy.inv


# --- each setting reaches the invocation ------------------------------------


def test_per_node_model_reaches_the_invocation(spy_executor):
    node = agent_node("n", "a")
    assert _capture(spy_executor, node, node_overrides={"n": {"model": "prov/big"}}).model == "prov/big"


def test_per_node_agent_dir_reaches_the_invocation(spy_executor):
    node = agent_node("n", "a")
    assert _capture(spy_executor, node, node_overrides={"n": {"agent_dir": "/work/x"}}).agent_dir == "/work/x"


def test_per_node_idle_timeout_bypasses_the_vocabulary(spy_executor):
    node = agent_node("n", "a", duration="long")
    # A raw idle_timeout_s on the node entry wins over the node's duration name.
    idle = _capture(spy_executor, node, node_overrides={"n": {"idle_timeout_s": 42}}, durations={"long": 900}).idle_timeout_s
    assert idle == 42


def test_per_node_duration_override_beats_the_flow_declared_one(spy_executor):
    node = agent_node("n", "a", duration="short")
    idle = _capture(spy_executor, node, node_overrides={"n": {"duration": "long"}}, durations={"short": 30, "long": 900}).idle_timeout_s
    assert idle == 900


def test_per_node_agent_dir_is_templated(spy_executor):
    node = agent_node("n", "a")
    ad = _capture(spy_executor, node, params={"root": "/w"}, node_overrides={"n": {"agent_dir": "{root}/defs"}}).agent_dir
    assert ad == "/w/defs"


# --- precedence: per-node run config > agent_node() arg > run-wide -----------


def test_per_node_model_beats_the_agent_node_arg(spy_executor):
    """The run config layer ("how THIS run behaves") is more specific than the
    flow's standing agent_node() declaration."""
    node = agent_node("n", "a", model="flow/declared")
    assert _capture(spy_executor, node, node_overrides={"n": {"model": "run/override"}}).model == "run/override"


def test_agent_node_arg_beats_run_wide_model(spy_executor):
    node = agent_node("n", "a", model="flow/declared")
    assert _capture(spy_executor, node, params={"model": "run/wide"}).model == "flow/declared"


def test_run_wide_model_used_when_nothing_more_specific(spy_executor):
    node = agent_node("n", "a")
    assert _capture(spy_executor, node, params={"model": "run/wide"}).model == "run/wide"


def test_empty_when_no_model_anywhere(spy_executor):
    node = agent_node("n", "a")
    assert _capture(spy_executor, node).model == ""


def test_per_node_agent_dir_beats_the_agent_node_arg(spy_executor):
    node = agent_node("n", "a", agent_dir="/flow/dir")
    assert _capture(spy_executor, node, node_overrides={"n": {"agent_dir": "/run/dir"}}).agent_dir == "/run/dir"


def test_a_partial_entry_inherits_the_rest(spy_executor):
    """A node entry that sets only `model` must not wipe agent_dir/idle."""
    node = agent_node("n", "a", agent_dir="/flow/dir")
    cap = _capture(spy_executor, node, params={"idle_timeout_s": "55"}, node_overrides={"n": {"model": "m"}})
    assert cap.model == "m"
    assert cap.agent_dir == "/flow/dir"  # inherited from the agent_node arg
    assert cap.idle_timeout_s == 55  # inherited from run-wide


# --- build-time validation --------------------------------------------------


def test_build_flow_rejects_an_unknown_node_key():
    with pytest.raises(ValueError, match="unknown node"):
        build_flow([agent_node("real", "a")], node_overrides={"typo": {"model": "m"}})


def test_build_flow_accepts_a_known_node_key():
    flow = build_flow([agent_node("real", "a")], node_overrides={"real": {"model": "m"}})
    assert callable(flow)


# --- NodeDef no longer carries the moved fields -----------------------------


def test_nodedef_rejects_model_and_agent_dir():
    from pydantic import ValidationError

    from agent_flow.flowdef import NodeDef

    with pytest.raises(ValidationError):
        NodeDef(name="n", agent="a", model="prov/x")
    with pytest.raises(ValidationError):
        NodeDef(name="n", agent="a", agent_dir="/x")


def test_noderunconfig_rejects_unknown_field():
    from pydantic import ValidationError

    from agent_flow import NodeRunConfig

    with pytest.raises(ValidationError):
        NodeRunConfig(modl="typo")  # extra=forbid


# --- BOTH entry points ------------------------------------------------------


@pytest.fixture
def flow_and_registry():
    from agent_flow.flowdef import FlowDef, NodeDef
    from agent_flow.registry import FlowRegistry

    seen: dict[str, str] = {}
    registry = FlowRegistry()

    @registry.mock_agent("a")
    def _capture(inv, ctx):  # noqa: ARG001
        seen["model"] = inv.model
        return {"status": "ok"}

    flow = FlowDef(name="t", nodes=[NodeDef(name="n", agent="a")])
    return flow, registry, seen


def test_programmatic_run_flow_delivers_node_overrides(flow_and_registry, tmp_path):
    from agent_flow import run_flow

    flow, registry, seen = flow_and_registry
    run_flow(flow, registry=registry, run_dir=str(tmp_path), mock_agents=True, run_config={"nodes": {"n": {"model": "prov/big"}}})
    assert seen["model"] == "prov/big"


def test_cli_path_delivers_node_overrides(flow_and_registry, tmp_path):
    from agent_flow.cli.commands.run import _build_and_run
    from agent_flow.cli.console import get_console
    from agent_flow.flowdef import compile_flow
    from agent_flow.run_config import build_run_config

    flow, registry, seen = flow_and_registry
    cfg = build_run_config(run_dir=str(tmp_path), mock_agents=True)
    cfg.set_node_instruction("n", "ignore")  # exercise the --instruct path
    cfg.nodes["n"] = cfg.nodes["n"].model_copy(update={"model": "prov/big"})
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
    assert seen["model"] == "prov/big", "run_cli must deliver the run config's `nodes:` overrides"


def test_build_flow_rejects_an_unknown_duration_in_an_override():
    """A duration typo in a `nodes:` override must fail at build, like a flow-
    declared one — build_flow has both in hand."""
    with pytest.raises(ValueError, match="unknown duration"):
        build_flow([agent_node("n", "a")], node_overrides={"n": {"duration": "lnog"}}, durations={"long": 900})
