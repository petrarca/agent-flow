"""Unit tests for the declaration-driven engine's pure orchestration logic.

These exercise plan_groups (DAG ordering + parallel grouping) and interpret
(gate directives, bounded re-runs, criticality) WITHOUT Prefect — the run
callables are plain functions. build_flow's Prefect wiring is covered by the
example running green in integration.
"""

from pathlib import Path

import pytest

from agent_flow.engine import Node, NodeBlocked, interpret, plan_groups
from agent_flow.gates import Continue, GoTo, Restart, Stop


def _noop(_ctx):
    return {"status": "ok"}


def test_plan_groups_orders_by_dependency():
    nodes = [
        Node("d", run=_noop, depends_on=("b", "c")),
        Node("a", run=_noop),
        Node("b", run=_noop, depends_on=("a",)),
        Node("c", run=_noop, depends_on=("a",)),
    ]
    plan = [(k, [n.name for n in g]) for k, g in plan_groups(nodes)]
    order = [k for k, _ in plan]
    # a before b/c before d
    assert order.index("a") < order.index("b")
    assert order.index("a") < order.index("c")
    assert order.index("d") == len(order) - 1


def test_plan_groups_merges_parallel_group():
    nodes = [
        Node("a", run=_noop),
        Node("b", run=_noop, depends_on=("a",), parallel_group="p"),
        Node("c", run=_noop, depends_on=("a",), parallel_group="p"),
    ]
    plan = plan_groups(nodes)
    groups = {k: [n.name for n in g] for k, g in plan}
    assert groups["p"] == ["b", "c"]  # both members in one group


def test_plan_groups_unknown_dependency():
    with pytest.raises(ValueError, match="unknown node"):
        plan_groups([Node("a", run=_noop, depends_on=("ghost",))])


def test_plan_groups_cycle():
    nodes = [
        Node("a", run=_noop, depends_on=("b",)),
        Node("b", run=_noop, depends_on=("a",)),
    ]
    with pytest.raises(ValueError, match="cycle"):
        plan_groups(nodes)


def _run(tmp_path, node):
    # interpret returns a NodeOutcome; most tests only care about the status.
    return interpret(node, run_dir=Path(tmp_path), params={}, on_error=_criticality).status


def _criticality(node, exc):
    if node.criticality == "blocking":
        raise NodeBlocked(f"{node.name}: {exc}") from exc
    return "degraded"


def test_interpret_continue_when_no_gate(tmp_path):
    assert _run(tmp_path, Node("a", run=_noop)) == "ok"


def test_interpret_restart_bounded(tmp_path):
    calls = {"n": 0}

    def run(_ctx):
        calls["n"] += 1
        return {}

    node = Node("a", run=run, gate=lambda ctx: Restart() if ctx.cycles < 1 else Continue(), max_cycles=1)
    assert _run(tmp_path, node) == "ok"
    assert calls["n"] == 2  # initial + 1 bounded restart


def test_interpret_restart_hits_cap(tmp_path):
    calls = {"n": 0}

    def run(_ctx):
        calls["n"] += 1
        return {}

    # Gate always asks to restart; max_cycles caps it.
    node = Node("a", run=run, gate=lambda _ctx: Restart(), max_cycles=2)
    assert _run(tmp_path, node) == "ok"
    assert calls["n"] == 3  # initial + 2 restarts, then stop trying


def test_interpret_goto_self_is_restart(tmp_path):
    calls = {"n": 0}

    def run(_ctx):
        calls["n"] += 1
        return {}

    node = Node("a", run=run, gate=lambda ctx: GoTo("a") if ctx.cycles < 1 else Continue(), max_cycles=1)
    assert _run(tmp_path, node) == "ok"
    assert calls["n"] == 2


def test_interpret_delivers_on_event_factory_to_runcontext(tmp_path):
    # on_event_factory is engine plumbing threaded via RunContext, NOT via params.
    marker = object()
    seen = {}

    def run(ctx):
        seen["factory_is_marker"] = ctx.on_event_factory is marker
        seen["params_has_no_on_event"] = "on_event_factory" not in ctx.params
        return {}

    interpret(Node("a", run=run), run_dir=Path(tmp_path), params={}, on_error=_criticality, on_event_factory=marker)
    assert seen["factory_is_marker"] is True
    assert seen["params_has_no_on_event"] is True


def test_interpret_stop_raises(tmp_path):
    node = Node("a", run=_noop, gate=lambda _ctx: Stop("halt"))
    with pytest.raises(NodeBlocked, match="halt"):
        _run(tmp_path, node)


def test_interpret_blocking_error_raises(tmp_path):
    def boom(_ctx):
        raise RuntimeError("kaboom")

    with pytest.raises(NodeBlocked, match="kaboom"):
        _run(tmp_path, Node("a", run=boom, criticality="blocking"))


def test_interpret_degrade_error_continues(tmp_path):
    def boom(_ctx):
        raise RuntimeError("kaboom")

    assert _run(tmp_path, Node("a", run=boom, criticality="degrade")) == "degraded"
