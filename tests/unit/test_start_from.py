"""Unit tests for the start_from forward entry point (begin at a node, skip upstream)."""

import pytest

from agent_flow.engine import _resolve_start_index, _walk
from agent_flow.engine.walker import _resolve_entry
from agent_flow.flow_types import NodeOutcome


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def debug(self, *a, **k):
        pass


def _linear(names):
    """A linear plan of solo groups + the index maps _walk/_resolve need."""
    planned = [(n, [n]) for n in names]
    group_index = {n: i for i, n in enumerate(names)}
    node_group = {n: n for n in names}
    by_name = {n: object() for n in names}
    return planned, group_index, node_group, by_name


# --- _resolve_start_index ---------------------------------------------------


def test_resolve_default_is_zero():
    _p, gi, ng, bn = _linear(["a", "b", "c"])
    assert _resolve_start_index("", bn, gi, ng, _Logger()) == 0


def test_resolve_translates_node_to_group_index():
    _p, gi, ng, bn = _linear(["a", "b", "c"])
    assert _resolve_start_index("c", bn, gi, ng, _Logger()) == 2


def test_resolve_unknown_name_raises():
    _p, gi, ng, bn = _linear(["a", "b", "c"])
    with pytest.raises(ValueError, match="not a known node or group"):
        _resolve_start_index("nope", bn, gi, ng, _Logger())


def test_resolve_accepts_group_name_and_member_node():
    # B and C share parallel_group "workers"; A and D are solo.
    group_index = {"a": 0, "workers": 1, "d": 2}
    node_group = {"A": "a", "B": "workers", "C": "workers", "D": "d"}
    by_name = {"A": 1, "B": 1, "C": 1, "D": 1}
    log = _Logger()
    assert _resolve_start_index("workers", by_name, group_index, node_group, log) == 1  # group name
    assert _resolve_start_index("B", by_name, group_index, node_group, log) == 1  # member node -> same group
    assert _resolve_start_index("D", by_name, group_index, node_group, log) == 2  # solo node


# --- _walk with start_index -------------------------------------------------


def _run_recording(ran):
    async def run_group(group, only_nodes=None):
        members = [n for n in group if only_nodes is None or n in only_nodes]
        ran.extend(members)
        return {n: NodeOutcome(status="ok") for n in members}

    return run_group


@pytest.mark.anyio
async def test_walk_from_zero_runs_all():
    planned, gi, ng, bn = _linear(["a", "b", "c"])
    ran: list = []
    await _walk(planned, run_group=_run_recording(ran), group_index=gi, node_group=ng, by_name=bn, logger=_Logger())
    assert ran == ["a", "b", "c"]


@pytest.mark.anyio
async def test_walk_start_index_skips_upstream_runs_forward():
    planned, gi, ng, bn = _linear(["a", "b", "c"])
    ran: list = []
    await _walk(planned, run_group=_run_recording(ran), group_index=gi, node_group=ng, by_name=bn, logger=_Logger(), start_index=1)
    assert ran == ["b", "c"]  # 'a' skipped; entry at 'b', forward from there


@pytest.mark.anyio
async def test_walk_start_at_last_runs_only_it():
    planned, gi, ng, bn = _linear(["a", "b", "c"])
    ran: list = []
    await _walk(planned, run_group=_run_recording(ran), group_index=gi, node_group=ng, by_name=bn, logger=_Logger(), start_index=2)
    assert ran == ["c"]


# --- stop_index (the --stop-after upper bound) ------------------------------


@pytest.mark.anyio
async def test_walk_stop_index_is_exclusive_upper_bound():
    planned, gi, ng, bn = _linear(["a", "b", "c", "d"])
    ran: list = []
    # stop_index=3 -> run groups [0,3): a, b, c — NOT d.
    await _walk(planned, run_group=_run_recording(ran), group_index=gi, node_group=ng, by_name=bn, logger=_Logger(), stop_index=3)
    assert ran == ["a", "b", "c"]


@pytest.mark.anyio
async def test_walk_start_and_stop_run_a_segment():
    planned, gi, ng, bn = _linear(["a", "b", "c", "d", "e"])
    ran: list = []
    # [start=1, stop=4) -> b, c, d (the b..d segment).
    await _walk(planned, run_group=_run_recording(ran), group_index=gi, node_group=ng, by_name=bn, logger=_Logger(), start_index=1, stop_index=4)
    assert ran == ["b", "c", "d"]


# --- _resolve_entry: the (start, stop, single_group) resolution -------------


def test_resolve_entry_stop_after_is_inclusive():
    _p, gi, ng, bn = _linear(["a", "b", "c", "d"])
    start, stop, single = _resolve_entry("", "", "c", bn, gi, ng, _Logger())
    assert (start, single) == (0, False)
    assert stop == 3  # inclusive of c's group (index 2) -> exclusive bound 3


def test_resolve_entry_start_and_stop_segment():
    _p, gi, ng, bn = _linear(["a", "b", "c", "d", "e"])
    start, stop, _ = _resolve_entry("b", "", "d", bn, gi, ng, _Logger())
    assert (start, stop) == (1, 4)  # groups [1,4) = b, c, d


def test_resolve_entry_stop_before_start_raises():
    _p, gi, ng, bn = _linear(["a", "b", "c"])
    with pytest.raises(ValueError, match="before start_from"):
        _resolve_entry("c", "", "a", bn, gi, ng, _Logger())


def test_resolve_entry_only_excludes_stop_after():
    _p, gi, ng, bn = _linear(["a", "b", "c"])
    with pytest.raises(ValueError, match="only is exclusive"):
        _resolve_entry("", "b", "c", bn, gi, ng, _Logger())


def test_resolve_entry_only_is_single_group():
    _p, gi, ng, bn = _linear(["a", "b", "c"])
    start, stop, single = _resolve_entry("", "b", "", bn, gi, ng, _Logger())
    assert (start, single) == (1, True)
