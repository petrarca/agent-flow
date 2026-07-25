"""Unit tests for the execution-backend seam.

Three layers, tested where each belongs:
  1. the FACTORY (get_backend) — name -> fresh instance;
  2. the shared TEMPLATE METHOD (FlowBackend.run_group) — tested once against a
     fake backend that stubs the parallel primitive, since the solo/parallel
     branch + degraded backfill is backend-agnostic;
  3. InProcessBackend's own primitives (threadpool _execute_parallel, semaphore,
     no-op lifecycle) — the parts that actually vary.

PrefectBackend's _execute_parallel submits real Prefect tasks (needs a temp
server), so its end-to-end behavior is covered in the integration suite
(test_flow_backends.py), parametrized alongside inprocess. This keeps the unit suite
Prefect-free (see test_prefect_isolation.py).
"""

import pytest

from agent_flow.backends import DEFAULT_BACKEND, FlowBackend, InProcessBackend, get_backend
from agent_flow.backends.base import RunNode
from agent_flow.engine import NodeBlocked, NodeOutcome

# --- factory ----------------------------------------------------------------


def test_default_is_local():
    assert DEFAULT_BACKEND == "inprocess"


def test_get_backend_inprocess():
    b = get_backend("inprocess")
    assert isinstance(b, InProcessBackend) and isinstance(b, FlowBackend) and b.name == "inprocess"


def test_get_backend_fresh_instances():
    # Fresh instance per call keeps per-run state (InProcessBackend's semaphore) isolated.
    assert get_backend("inprocess") is not get_backend("inprocess")


def test_get_backend_prefect_constructs_without_running():
    # Lazy prefect import happens inside the factory; constructing succeeds
    # (imports the class) without running a flow.
    b = get_backend("prefect")
    assert b.name == "prefect" and isinstance(b, FlowBackend)


def test_get_backend_unknown_raises():
    with pytest.raises(ValueError, match="unknown backend 'nope'"):
        get_backend("nope")


# --- shared template method: FlowBackend.run_group --------------------------
# Tested against a FAKE backend so we exercise the base-class branch/backfill
# logic ONCE, independent of any concrete backend's concurrency primitive.


class _FakeBackend(FlowBackend):
    """Minimal concrete backend: _execute_parallel just calls run_node serially."""

    name = "fake"

    def run_session(self, name, work):
        return work()

    def _execute_parallel(self, node_names, run_node: RunNode):
        out = {}
        for name in node_names:
            try:
                out[name] = run_node(name)
            except NodeBlocked:
                raise
            except Exception:  # noqa: BLE001
                out[name] = NodeOutcome(status="degraded")
        return out

    def apply_concurrency_limit(self, tag, limit, info, warn):  # pragma: no cover
        pass

    def get_logger(self):
        import logging

        return logging.getLogger("agent_flow")

    def bootstrap(self):  # pragma: no cover
        pass

    def teardown(self):  # pragma: no cover
        pass


class _Node:  # run_group only reads .name
    def __init__(self, name):
        self.name = name


def _run_node_factory(record, fail=(), block=()):
    def run_node(name):
        record.append(name)
        if name in block:
            raise NodeBlocked(f"{name}: fatal")
        if name in fail:
            raise RuntimeError("boom")
        return NodeOutcome(status="ok")

    return run_node


def test_run_group_solo_inline():
    ran = []
    out = _FakeBackend().run_group([_Node("a")], _run_node_factory(ran))
    assert ran == ["a"] and out["a"].status == "ok"


def test_run_group_parallel_runs_all():
    ran = []
    out = _FakeBackend().run_group([_Node("p1"), _Node("p2"), _Node("p3")], _run_node_factory(ran))
    assert set(out) == {"p1", "p2", "p3"} and all(o.status == "ok" for o in out.values())


def test_run_group_backfills_missing_name_as_degraded():
    # A backend that drops a node -> run_group backfills it to degraded (never lost).
    class _Dropping(_FakeBackend):
        def _execute_parallel(self, node_names, run_node):
            return {node_names[0]: NodeOutcome(status="ok")}  # omit the rest

    out = _Dropping().run_group([_Node("a"), _Node("b")], _run_node_factory([]))
    assert out["a"].status == "ok" and out["b"].status == "degraded"


def test_run_group_propagates_node_blocked():
    with pytest.raises(NodeBlocked):
        _FakeBackend().run_group([_Node("ok"), _Node("block")], _run_node_factory([], block=("block",)))


# --- InProcessBackend-specific primitives ---------------------------------------


def test_local_parallel_threadpool_runs_all():
    ran = []
    out = InProcessBackend()._execute_parallel(["p1", "p2", "p3"], _run_node_factory(ran))
    assert set(out) == {"p1", "p2", "p3"} and all(o.status == "ok" for o in out.values())


def test_local_parallel_degrades_on_exception():
    out = InProcessBackend()._execute_parallel(["ok", "bad"], _run_node_factory([], fail=("bad",)))
    assert out["ok"].status == "ok" and out["bad"].status == "degraded"


def test_local_parallel_propagates_node_blocked():
    with pytest.raises(NodeBlocked):
        InProcessBackend()._execute_parallel(["ok", "block"], _run_node_factory([], block=("block",)))


def test_local_concurrency_limit_sets_semaphore():
    b = InProcessBackend()
    assert b._sema is None
    msgs = []
    b.apply_concurrency_limit("llm", 2, msgs.append, msgs.append)
    assert b._sema is not None and any("2" in m for m in msgs)


def test_local_bootstrap_teardown_are_noops():
    b = InProcessBackend()
    assert b.bootstrap() is None and b.teardown() is None
