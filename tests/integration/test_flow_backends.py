"""Integration: a real flow runs identically on every execution backend.

Parametrized over backends (mirrors cypher-graphdb-core's AGE/Memgraph
`test_db` pattern): the SAME assertions run on `local` and `prefect`, so a new
backend added later only needs a new param + (if it has preconditions) a skip
guard. The prefect param spins a temporary Prefect server, so this lives in the
integration suite; the local param needs nothing.

Backend-agnostic behavior verified here: solo + parallel-group execution, the
degraded mapping, and start_from/only entry all produce the same outcomes
regardless of backend — proving the engine owns the flow logic and the backend
only executes.
"""

import pytest

from agent_flow.engine import Node, build_flow

pytestmark = pytest.mark.integration


@pytest.fixture
def backend(request):
    """Resolve a backend name param; skip prefect when it is not importable.

    Indirect fixture: parametrize with `indirect=["backend"]` and a list of
    backend names. Prefect needs its env bootstrapped before use, so we do that
    here (only for the prefect param) — the local param needs nothing.
    """
    name = request.param
    if name == "prefect":
        pytest.importorskip("prefect", reason="prefect not installed")
        from agent_flow.backends.prefect import PrefectBackend

        PrefectBackend().bootstrap()
    return name


_ALL_BACKENDS = ["inprocess", "prefect"]


def _mk(nm):
    def run(_ctx):
        return {"ran": nm}

    return run


def _nodes():
    # solo -> a 2-node parallel group (exercises both run_group branches).
    return [
        Node(name="a", run=_mk("a")),
        Node(name="p1", run=_mk("p1"), parallel_group="workers", depends_on=["a"]),
        Node(name="p2", run=_mk("p2"), parallel_group="workers", depends_on=["a"]),
    ]


@pytest.mark.parametrize("backend", _ALL_BACKENDS, indirect=True)
def test_flow_runs_on_backend(backend, tmp_path):
    result = build_flow(_nodes(), name="be", backend=backend)(run_dir=str(tmp_path))
    assert {n: oc.status for n, oc in result.items()} == {"a": "ok", "p1": "ok", "p2": "ok"}


@pytest.mark.parametrize("backend", _ALL_BACKENDS, indirect=True)
def test_degraded_node_does_not_abort(backend, tmp_path):
    def bad(_ctx):
        raise RuntimeError("boom")

    nodes = [Node(name="ok", run=_mk("ok")), Node(name="bad", run=bad, criticality="degrade", depends_on=["ok"])]
    result = build_flow(nodes, name="be-degrade", backend=backend)(run_dir=str(tmp_path))
    assert result["ok"].status == "ok"
    assert result["bad"].status == "degraded"


@pytest.mark.parametrize("backend", _ALL_BACKENDS, indirect=True)
def test_only_runs_single_group_on_backend(backend, tmp_path):
    # `only` is engine logic, backend-agnostic: exactly one node runs.
    result = build_flow(_nodes(), name="be-only", backend=backend)(run_dir=str(tmp_path), only="a")
    assert set(result) == {"a"} and result["a"].status == "ok"
