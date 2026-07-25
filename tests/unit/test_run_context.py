"""Unit tests for the run-context service and the result->params exports hook."""

import pytest

from agent_flow.engine import Node, interpret
from agent_flow.gates import GoTo
from agent_flow.run_context import (
    RunContextService,
    clear_run_context,
    get_run_context,
    init_run_context,
)


@pytest.fixture(autouse=True)
def _clean_run_context():
    clear_run_context()
    yield
    clear_run_context()


# --- the service ------------------------------------------------------------


def test_service_get_set_update_snapshot():
    svc = RunContextService({"a": "1"})
    assert svc.get("a") == "1"
    assert svc.get("missing", "d") == "d"
    svc.set("b", "2")
    svc.update({"c": "3"}, d="4")
    snap = svc.snapshot()
    assert snap == {"a": "1", "b": "2", "c": "3", "d": "4"}
    # snapshot is a copy — mutating it does not touch the store
    snap["a"] = "x"
    assert svc.get("a") == "1"


def test_update_empty_is_noop():
    svc = RunContextService({"a": "1"})
    svc.update()
    svc.update({})
    assert svc.snapshot() == {"a": "1"}


def test_singleton_lifecycle():
    a = get_run_context()  # empty default
    assert a.snapshot() == {}
    b = init_run_context({"product_key": "demo"})
    assert get_run_context() is b
    assert get_run_context().get("product_key") == "demo"
    clear_run_context()
    assert get_run_context().snapshot() == {}


# --- exports hook via interpret --------------------------------------------


def _node(name, run, **kw):
    return Node(name=name, run=run, **kw)


def test_declarative_exports_merge_into_run_context():
    init_run_context({"product_key": "demo"})

    def run(ctx):
        # a node's result carries fields the flow wants downstream
        return {"analysis_timestamp": "2026-07-25T00:00:00Z", "pipeline_commit": "abc1234", "status": "ok"}

    node = _node("readiness", run, exports={"analysis_timestamp": "analysis_timestamp", "pipeline_commit": "pipeline_commit"})
    out = interpret(node, run_dir=__import__("pathlib").Path("/tmp"), params={"product_key": "demo"}, on_error=lambda n, e: "degraded")
    assert out.status == "ok"
    ctx = get_run_context()
    assert ctx.get("analysis_timestamp") == "2026-07-25T00:00:00Z"
    assert ctx.get("pipeline_commit") == "abc1234"
    # a field not named in the map is NOT exported
    assert ctx.get("status") is None


def test_callable_exports_full_control():
    init_run_context({})

    def run(ctx):
        return {"result": {"mode": "validation"}}

    node = _node("readiness", run, exports=lambda r: {"mode": r["result"]["mode"].upper()})
    interpret(node, run_dir=__import__("pathlib").Path("/tmp"), params={}, on_error=lambda n, e: "degraded")
    assert get_run_context().get("mode") == "VALIDATION"


def test_exports_reads_typed_object_when_result_schema_set():
    # When a result_schema is used, the result dict carries the validated object
    # under _result_obj; exports (declarative AND callable) see the TYPED object,
    # so a consumer reads attributes directly — no _result_obj key digging.
    import types

    init_run_context({})
    obj = types.SimpleNamespace(pipeline_commit="abc1234", ready="yes")

    def run(ctx):
        return {"status": "ok", "_result_obj": obj}

    # declarative: field name resolves as an ATTRIBUTE on the typed object
    node = _node("readiness", run, exports={"pipeline_commit": "pipeline_commit"})
    interpret(node, run_dir=__import__("pathlib").Path("/tmp"), params={}, on_error=lambda n, e: "degraded")
    assert get_run_context().get("pipeline_commit") == "abc1234"

    # callable: receives the typed object directly
    clear_run_context()
    init_run_context({})
    node2 = _node("r2", run, exports=lambda o: {"ready": o.ready})
    interpret(node2, run_dir=__import__("pathlib").Path("/tmp"), params={}, on_error=lambda n, e: "degraded")
    assert get_run_context().get("ready") == "yes"


def test_missing_result_field_is_skipped_not_error():
    init_run_context({})

    def run(ctx):
        return {"status": "ok"}  # no 'mode' field

    node = _node("n", run, exports={"mode": "mode"})
    out = interpret(node, run_dir=__import__("pathlib").Path("/tmp"), params={}, on_error=lambda n, e: "degraded")
    assert out.status == "ok"
    assert get_run_context().get("mode") is None


def test_exports_error_is_ignored():
    init_run_context({})

    def run(ctx):
        return {"x": 1}

    def bad(_r):
        raise RuntimeError("boom")

    node = _node("n", run, exports=bad)
    out = interpret(node, run_dir=__import__("pathlib").Path("/tmp"), params={}, on_error=lambda n, e: "degraded")
    assert out.status == "ok"  # exports failure never fails the node


def test_downstream_node_sees_exported_value_in_params():
    init_run_context({"product_key": "demo"})
    seen = {}

    def upstream(ctx):
        return {"mode": "validation"}

    def downstream(ctx):
        # eff_params overlays the run-context snapshot -> sees the export
        seen["mode"] = ctx.params.get("mode")
        return {"status": "ok"}

    up = _node("up", upstream, exports={"mode": "mode"})
    interpret(up, run_dir=__import__("pathlib").Path("/tmp"), params={"product_key": "demo"}, on_error=lambda n, e: "degraded")
    down = _node("down", downstream)
    interpret(down, run_dir=__import__("pathlib").Path("/tmp"), params={"product_key": "demo"}, on_error=lambda n, e: "degraded")
    assert seen["mode"] == "validation"


def test_exports_applied_on_cross_node_goto_too():
    init_run_context({})

    def run(ctx):
        return {"mode": "migration"}

    node = _node("n", run, exports={"mode": "mode"}, gate=lambda ctx: GoTo("other"))
    out = interpret(node, run_dir=__import__("pathlib").Path("/tmp"), params={}, on_error=lambda n, e: "degraded")
    assert out.goto == "other"
    assert get_run_context().get("mode") == "migration"  # published before the jump
