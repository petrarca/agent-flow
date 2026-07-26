"""Unit tests for the FlowDef declarative surface + compile_flow."""

import pytest

from agent_flow import FlowDef, FlowRegistry, NodeDef, compile_flow
from agent_flow.gates import Continue

# --- NodeDef / FlowDef validation -------------------------------------------


def test_nodedef_requires_exactly_one_run_source():
    NodeDef(name="a", agent="x")  # ok
    NodeDef(name="a", run_ref="r")  # ok
    with pytest.raises(ValueError, match="exactly one of `agent` or `run_ref`"):
        NodeDef(name="a")
    with pytest.raises(ValueError, match="exactly one of `agent` or `run_ref`"):
        NodeDef(name="a", agent="x", run_ref="r")


def test_nodedef_exports_xor_export_ref():
    with pytest.raises(ValueError, match="at most one of `exports` or `export_ref`"):
        NodeDef(name="a", agent="x", exports={"k": "f"}, export_ref="e")


def test_flowdef_rejects_duplicate_names():
    with pytest.raises(ValueError, match="duplicate node names"):
        FlowDef(name="f", nodes=[NodeDef(name="a", agent="x"), NodeDef(name="a", agent="y")])


def test_flowdef_rejects_unknown_dependency():
    with pytest.raises(ValueError, match="unknown node 'ghost'"):
        FlowDef(name="f", nodes=[NodeDef(name="a", agent="x", depends_on=["ghost"])])


def test_flowdef_to_json_roundtrip():
    flow = FlowDef(name="f", nodes=[NodeDef(name="a", agent="x")])
    js = flow.to_json()
    assert '"name": "a"' in js and '"agent": "x"' in js
    assert FlowDef.model_validate_json(js) == flow


# --- compile_flow -----------------------------------------------------------


def test_compile_agent_node():
    flow = FlowDef(name="f", nodes=[NodeDef(name="a", agent="x", gate="require_file", gate_args={"path": "r.md"})])
    nodes = compile_flow(flow, FlowRegistry())
    assert len(nodes) == 1
    n = nodes[0]
    assert n.name == "a" and n.agent == "x" and n.gate_ref == "require_file" and n.gate_args == {"path": "r.md"}


def test_compile_custom_run_ref():
    reg = FlowRegistry()

    @reg.run("fetch")
    def _fetch(ctx):
        return {"got": True}

    flow = FlowDef(name="f", nodes=[NodeDef(name="a", run_ref="fetch")])
    nodes = compile_flow(flow, reg)
    assert nodes[0].run is _fetch and nodes[0].agent == ""


def test_compile_resolves_schema_by_name():
    reg = FlowRegistry()

    class S:  # any object accepted by run_agent's coerce_schema; identity check here
        pass

    reg.schema("MySchema")(S)
    flow = FlowDef(name="f", nodes=[NodeDef(name="a", agent="x", result_schema="MySchema")])
    nodes = compile_flow(flow, reg)
    assert nodes[0].result_schema is S


def test_compile_unknown_gate_raises():
    flow = FlowDef(name="f", nodes=[NodeDef(name="a", agent="x", gate="nope")])
    with pytest.raises(ValueError, match="unknown gate 'nope'"):
        compile_flow(flow, FlowRegistry())


def test_compile_unknown_run_ref_raises():
    flow = FlowDef(name="f", nodes=[NodeDef(name="a", run_ref="missing")])
    with pytest.raises(ValueError, match="unknown run 'missing'"):
        compile_flow(flow, FlowRegistry())


def test_custom_gate_by_name_via_registry():
    reg = FlowRegistry()

    @reg.gate("mygate")
    def _mygate(ctx):
        return Continue()

    flow = FlowDef(name="f", nodes=[NodeDef(name="a", agent="x", gate="mygate")])
    nodes = compile_flow(flow, reg)  # must not raise (gate known)
    assert nodes[0].gate_ref == "mygate"
