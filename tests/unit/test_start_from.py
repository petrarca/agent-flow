"""Unit tests for the start_from forward entry point (begin at a node, skip upstream)."""

import pytest

from agent_flow.engine import _resolve_start_index, _walk
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
    async def run_group(group):
        ran.append(group[0])
        return {group[0]: NodeOutcome(status="ok")}

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
