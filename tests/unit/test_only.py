"""Unit tests for the `only` mode (run a SINGLE node/group and stop).

Complement to start_from: start_from runs from a group to the end; only runs
exactly one group. Same GROUP granularity (a parallel group is indivisible);
no forward advance and no jump-backs.
"""

import pytest

from agent_flow.engine import NodeOutcome, _resolve_only_index, _walk


class _Logger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass


def _linear(names):
    """A linear plan of solo groups + the index maps _walk/_resolve need."""
    planned = [(n, [n]) for n in names]
    group_index = {n: i for i, n in enumerate(names)}
    node_group = {n: n for n in names}
    by_name = {n: object() for n in names}
    return planned, group_index, node_group, by_name


# --- _resolve_only_index ----------------------------------------------------


def test_resolve_only_translates_node_to_group_index():
    _p, gi, ng, bn = _linear(["a", "b", "c"])
    assert _resolve_only_index("b", bn, gi, ng, _Logger()) == 1


def test_resolve_only_unknown_name_raises():
    _p, gi, ng, bn = _linear(["a", "b", "c"])
    with pytest.raises(ValueError, match="not a known node or group"):
        _resolve_only_index("nope", bn, gi, ng, _Logger())


def test_resolve_only_accepts_group_name_and_member_node():
    # B and C share parallel_group "workers"; A and D are solo.
    group_index = {"a": 0, "workers": 1, "d": 2}
    node_group = {"A": "a", "B": "workers", "C": "workers", "D": "d"}
    by_name = {"A": 1, "B": 1, "C": 1, "D": 1}
    log = _Logger()
    assert _resolve_only_index("workers", by_name, group_index, node_group, log) == 1  # group name
    assert _resolve_only_index("C", by_name, group_index, node_group, log) == 1  # member node -> same group
    assert _resolve_only_index("A", by_name, group_index, node_group, log) == 0  # solo node


# --- _walk with single_group ------------------------------------------------


def _run_recording(ran):
    async def run_group(group):
        ran.append(group[0])
        return {group[0]: NodeOutcome(status="ok")}

    return run_group


@pytest.mark.anyio
async def test_walk_single_group_runs_only_that_group():
    planned, gi, ng, bn = _linear(["a", "b", "c"])
    ran: list = []
    await _walk(planned, run_group=_run_recording(ran), group_index=gi, node_group=ng, by_name=bn, logger=_Logger(), start_index=1, single_group=True)
    assert ran == ["b"]  # not "c" — no forward advance


@pytest.mark.anyio
async def test_walk_single_group_at_start_does_not_run_forward():
    planned, gi, ng, bn = _linear(["a", "b", "c"])
    ran: list = []
    await _walk(planned, run_group=_run_recording(ran), group_index=gi, node_group=ng, by_name=bn, logger=_Logger(), start_index=0, single_group=True)
    assert ran == ["a"]


@pytest.mark.anyio
async def test_walk_single_group_ignores_jump_back():
    # Even if the only group's gate asks to jump back, `only` mode stops after it.
    planned, gi, ng, bn = _linear(["a", "b", "c"])

    async def run_group(group):
        return {group[0]: NodeOutcome(status="ok", goto="a")}  # would rewind normally

    ran_calls = {"n": 0}

    async def counting(group):
        ran_calls["n"] += 1
        return await run_group(group)

    await _walk(planned, run_group=counting, group_index=gi, node_group=ng, by_name=bn, logger=_Logger(), start_index=1, single_group=True)
    assert ran_calls["n"] == 1  # ran the one group once; goto ignored


@pytest.mark.anyio
async def test_walk_parallel_group_runs_all_members_once():
    # workers = {b1, b2}; single_group runs both, then stops.
    planned = [("a", ["a"]), ("workers", ["b1", "b2"]), ("c", ["c"])]
    group_index = {"a": 0, "workers": 1, "c": 2}
    node_group = {"a": "a", "b1": "workers", "b2": "workers", "c": "c"}
    by_name = {"a": 1, "b1": 1, "b2": 1, "c": 1}
    ran: list = []

    async def run_group(group):
        ran.extend(n for n in group)
        return {n: NodeOutcome(status="ok") for n in group}

    await _walk(
        planned,
        run_group=run_group,
        group_index=group_index,
        node_group=node_group,
        by_name=by_name,
        logger=_Logger(),
        start_index=1,
        single_group=True,
    )
    assert ran == ["b1", "b2"]  # whole group, then stop (no "c")
