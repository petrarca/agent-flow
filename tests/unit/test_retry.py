"""Unit tests for the transient-failure RETRY policy.

An agent that hangs (stale-killed on the liveness deadline) or whose process
crashes is a TRANSIENT failure: nothing was wrong with the request, so the engine
re-runs the node, bounded by `max_retries`. A failure the agent DIAGNOSED itself
is PERMANENT and never retried. When the budget is spent, the node's
`criticality` decides — degrade continues the run, blocking stops it.

The retry lives in `interpret`, which runs inside the node's OWN task, so it is
isolated per agent run: a retried node's parallel siblings keep their outcomes.
"""

from __future__ import annotations

import anyio
import pytest

from agent_flow.engine import build_flow
from agent_flow.errors import PermanentAgentError, TransientAgentError
from agent_flow.flow_types import Node, NodeBlocked


def _counting(calls: dict, name: str, fail_times: int, exc: type[Exception] = TransientAgentError):
    """A node `run` that fails `fail_times` times, then succeeds. Counts attempts."""
    calls[name] = 0

    async def run(ctx):
        calls[name] += 1
        if calls[name] <= fail_times:
            raise exc(f"{name} failed on attempt {calls[name]}")
        return {"status": "ok"}

    return run


def _run(nodes, tmp_path, **kw):
    return anyio.run(lambda: build_flow(nodes, **kw)(run_dir=str(tmp_path)))


# --- the core policy ---------------------------------------------------------


def test_transient_failure_is_retried_and_succeeds(tmp_path):
    calls: dict = {}
    out = _run([Node("a", run=_counting(calls, "a", 1), criticality="degrade")], tmp_path)
    assert out["a"].status == "ok"
    assert calls["a"] == 2  # failed once, succeeded on the first retry


def test_transient_failure_is_bounded_then_degrades(tmp_path):
    # Always fails -> the budget is spent, then criticality decides (degrade).
    calls: dict = {}
    out = _run([Node("a", run=_counting(calls, "a", 99), criticality="degrade")], tmp_path)
    assert out["a"].status == "degraded"
    assert calls["a"] == 3  # 1 + the two default retries, then it gives up


def test_permanent_failure_is_never_retried(tmp_path):
    # A failure the agent diagnosed itself repeats identically — retrying only
    # burns tokens, so it goes straight to criticality.
    calls: dict = {}
    out = _run([Node("a", run=_counting(calls, "a", 99, PermanentAgentError), criticality="degrade")], tmp_path)
    assert out["a"].status == "degraded"
    assert calls["a"] == 1


def test_unknown_exception_is_treated_as_permanent(tmp_path):
    # Consumer code raising something unclassified: we cannot know whether
    # repeating its side effects is safe, so do not retry.
    calls: dict = {}
    out = _run([Node("a", run=_counting(calls, "a", 99, ValueError), criticality="degrade")], tmp_path)
    assert out["a"].status == "degraded"
    assert calls["a"] == 1


# --- the budget --------------------------------------------------------------


def test_max_retries_run_wide(tmp_path):
    calls: dict = {}
    out = _run([Node("a", run=_counting(calls, "a", 3), criticality="degrade")], tmp_path, max_retries=3)
    assert out["a"].status == "ok"
    assert calls["a"] == 4


def test_max_retries_zero_disables_retrying(tmp_path):
    calls: dict = {}
    out = _run([Node("a", run=_counting(calls, "a", 99), criticality="degrade")], tmp_path, max_retries=0)
    assert out["a"].status == "degraded"
    assert calls["a"] == 1


def test_per_node_run_config_overrides_run_wide(tmp_path):
    # nodes.<n>.max_retries beats the run-wide value (standard precedence).
    calls: dict = {}
    out = _run(
        [Node("a", run=_counting(calls, "a", 2), criticality="degrade")],
        tmp_path,
        max_retries=1,
        node_overrides={"a": {"max_retries": 2}},
    )
    assert out["a"].status == "ok"
    assert calls["a"] == 3


def test_negative_max_retries_is_clamped(tmp_path):
    calls: dict = {}
    _run([Node("a", run=_counting(calls, "a", 99), criticality="degrade")], tmp_path, max_retries=-5)
    assert calls["a"] == 1  # clamped to 0, never loops


# --- criticality after the budget is spent -----------------------------------


def test_blocking_node_retries_then_stops_the_run(tmp_path):
    calls: dict = {}
    with pytest.raises(NodeBlocked):
        _run([Node("a", run=_counting(calls, "a", 99), criticality="blocking")], tmp_path)
    assert calls["a"] == 3  # it still gets its retries before halting the run


# --- isolation: the property that matters in a parallel group ----------------


def test_retry_is_isolated_per_node_in_a_parallel_group(tmp_path):
    # Mirrors the real failure: one verifier in a 3-wide group hangs. It must
    # retry ALONE — its siblings already produced outcomes and must not re-run.
    calls: dict = {}
    nodes = [
        Node("seed", run=_counting(calls, "seed", 0)),
        Node("domain-verify", run=_counting(calls, "domain-verify", 0), depends_on=("seed",), parallel_group="verify", criticality="degrade"),
        Node("arch-verify", run=_counting(calls, "arch-verify", 1), depends_on=("seed",), parallel_group="verify", criticality="degrade"),
        Node("coupling-verify", run=_counting(calls, "coupling-verify", 0), depends_on=("seed",), parallel_group="verify", criticality="degrade"),
    ]
    out = _run(nodes, tmp_path)

    assert all(o.status == "ok" for o in out.values()), {n: o.status for n, o in out.items()}
    assert calls["arch-verify"] == 2  # the hung node retried
    assert calls["domain-verify"] == 1  # siblings ran exactly once
    assert calls["coupling-verify"] == 1


def test_exhausted_retry_in_a_group_degrades_only_that_node(tmp_path):
    # A node that never recovers degrades on its own; the group's other members
    # are unaffected and the flow proceeds.
    calls: dict = {}
    nodes = [
        Node("seed", run=_counting(calls, "seed", 0)),
        Node("ok-node", run=_counting(calls, "ok-node", 0), depends_on=("seed",), parallel_group="g", criticality="degrade"),
        Node("bad-node", run=_counting(calls, "bad-node", 99), depends_on=("seed",), parallel_group="g", criticality="degrade"),
    ]
    out = _run(nodes, tmp_path)

    assert out["ok-node"].status == "ok"
    assert out["bad-node"].status == "degraded"
    assert calls["ok-node"] == 1
    assert calls["bad-node"] == 3  # 1 + two retries, then degraded


# --- the two budgets are independent -----------------------------------------


def test_retries_do_not_consume_the_gate_cycle_budget(tmp_path):
    # `cycles` (gate re-runs) and `retries` (infrastructure) are separate: a node
    # that crashed once must still be able to answer a gate's Restart.
    from agent_flow.gates import Continue, Restart

    calls: dict = {}
    calls["a"] = 0
    seen: list[int] = []

    async def run(ctx):
        calls["a"] += 1
        if calls["a"] == 1:
            raise TransientAgentError("crashed")  # consumes a RETRY, not a cycle
        return {"status": "ok"}

    def gate(ctx):
        seen.append(ctx.cycles)
        # bounce once via the gate, after the transient failure already happened
        return Restart() if len(seen) == 1 else Continue()

    out = _run([Node("a", run=run, gate=gate, criticality="degrade", max_cycles=1)], tmp_path)
    assert out["a"].status == "ok"
    # 1 crashed attempt + 1 retry (reached the gate) + 1 gate restart = 3 runs
    assert calls["a"] == 3
    assert seen == [0, 1]  # the gate saw cycle 0 then cycle 1 — retries did not inflate it


# --- the one-time instruction must survive a transient failure ----------------


def test_retry_keeps_the_one_time_instruction(tmp_path):
    """A hung/crashed attempt never HAPPENED, so its instruction is not spent.

    The carrier is cleared before each attempt (the instruction is single-attempt
    by design), but dropping it on a transient failure would make the retry redo
    the ORIGINAL work instead of the corrected work a jump-back asked for — the
    one case the instruction exists for.
    """
    seen: list[str] = []

    def verify(ctx):
        # First pass asks its subject to re-run, with guidance (the analyst has
        # run once by now, so `seen` already holds its first, blank instruction).
        if len(seen) == 1:
            return {"status": "verified", "rerun_required": {"instruction": "recompute the coupling figure"}}
        return {"status": "verified"}

    attempts = {"n": 0}

    async def analyst(ctx):
        seen.append(ctx.one_time_instruction)
        attempts["n"] += 1
        # Hang on the FIRST re-run attempt (the one carrying the instruction).
        if attempts["n"] == 2:
            raise TransientAgentError("analyst went stale")
        return {"status": "ok"}

    nodes = [
        Node("analyst", run=analyst, criticality="degrade", max_cycles=2),
        Node("verify", run=verify, depends_on=("analyst",), rerun_targets=("analyst",), criticality="degrade"),
    ]
    _run(nodes, tmp_path)

    # 1st run: no instruction. 2nd (jump-back): instruction, but the agent hung.
    # 3rd (retry of that attempt): the instruction must STILL be there.
    assert seen[0] == ""
    assert seen[1] == "recompute the coupling figure"
    assert seen[2] == "recompute the coupling figure"
