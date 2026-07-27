"""Unit tests for `input_schema` — typed node INPUTS, the mirror of result_schema.

The model: `inputs` carries the VALUES (templated, per node), `input_schema`
carries their TYPE (shared, referenced by name in a FlowDef). Validation runs on
the RESOLVED work order — after `{param}` templating and upstream `exports` — so
an unresolved placeholder fails before an agent is ever spawned.
"""

import json
import tempfile

import anyio
import pytest
from pydantic import BaseModel, ConfigDict, Field

from agent_flow import agent_node, build_flow
from agent_flow.engine import Node
from agent_flow.flowdef import FlowDef, NodeDef, compile_flow
from agent_flow.registry import FlowRegistry


class TriageIn(BaseModel):
    ticket: str
    priority: str = "normal"


class AliasedIn(BaseModel):
    """snake_case fields, UPPERCASE wire keys — keeps the prompt convention."""

    model_config = ConfigDict(populate_by_name=True)
    ticket: str = Field(validation_alias="TICKET")
    priority: str = Field(validation_alias="PRIORITY", default="normal")


def _run(nodes, **params):
    with tempfile.TemporaryDirectory() as d:
        params.setdefault("run_dir", d)
        return anyio.run(lambda: build_flow(nodes, name="t")(**params))


# --- the typed instance reaches the impl ------------------------------------


def test_validated_instance_is_passed_to_the_impl():
    seen = {}

    async def impl(inv):
        seen["obj"] = inv.input_obj
        seen["inputs"] = inv.inputs
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl, input_schema=TriageIn, inputs={"ticket": "{ticket}", "priority": "high"})
    out = _run([n], ticket="login crash")

    assert out["n"].status == "ok"
    assert isinstance(seen["obj"], TriageIn)
    assert seen["obj"].ticket == "login crash" and seen["obj"].priority == "high"
    assert seen["inputs"] == {"ticket": "login crash", "priority": "high"}  # raw values still there


def test_no_input_schema_leaves_input_obj_none():
    seen = {}

    async def impl(inv):
        seen["obj"] = inv.input_obj
        return {"status": "ok"}

    _run([agent_node("n", "a", impl=impl, inputs={"ticket": "x"})])
    assert seen["obj"] is None


def test_defaults_are_applied_by_the_schema():
    seen = {}

    async def impl(inv):
        seen["obj"] = inv.input_obj
        return {"status": "ok"}

    _run([agent_node("n", "a", impl=impl, input_schema=TriageIn, inputs={"ticket": "x"})])
    assert seen["obj"].priority == "normal"  # not supplied in inputs


# --- validation failure routes through criticality --------------------------


def test_missing_required_input_fails_the_node():
    ran = []

    async def impl(inv):
        ran.append(1)
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl, input_schema=TriageIn, inputs={"priority": "high"}, criticality="degrade")
    out = _run([n])
    assert out["n"].status == "degraded"
    assert ran == []  # the agent was never invoked


def test_unresolved_export_is_caught_instead_of_reaching_the_agent():
    """The payoff: `{mode}` that never resolved is a schema error, not prompt text."""

    class NeedsMode(BaseModel):
        mode: str = Field(pattern="^(deep|quick)$")

    ran = []

    async def impl(inv):
        ran.append(1)
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl, input_schema=NeedsMode, inputs={"mode": "{mode}"}, criticality="degrade")
    assert _run([n])["n"].status == "degraded"
    assert ran == []


def test_blocking_node_halts_the_run_on_invalid_input():
    from agent_flow.engine import NodeBlocked

    async def impl(inv):
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl, input_schema=TriageIn, inputs={"priority": "high"})  # blocking (default)
    with pytest.raises(NodeBlocked):
        _run([n])


# --- rendering is NOT changed by typing -------------------------------------


def test_prompt_keys_are_unchanged_by_input_schema():
    """Typing must not re-render the work order: an existing agent's .md refers
    to the authored keys (UPPERCASE by convention), so aliases carry the mapping."""
    seen = {}

    async def impl(inv):
        seen["prompt"] = inv.prompt
        seen["obj"] = inv.input_obj
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl, input_schema=AliasedIn, inputs={"TICKET": "{ticket}", "PRIORITY": "high"})
    _run([n], ticket="login crash")

    assert "TICKET: login crash" in seen["prompt"]  # authored casing preserved
    assert seen["obj"].ticket == "login crash"  # snake_case field, via alias


# --- values still flow between nodes ----------------------------------------


def test_upstream_exports_feed_a_typed_input():
    class AnalysisIn(BaseModel):
        mode: str
        score: int  # arrives as a string through templating; pydantic coerces

    seen = {}

    async def upstream(ctx):
        return {"status": "ok", "mode": "deep", "score": 7}

    async def impl(inv):
        seen["obj"] = inv.input_obj
        return {"status": "ok"}

    up = Node("up", run=upstream, exports={"mode": "mode", "score": "score"})
    down = agent_node("down", "a", impl=impl, depends_on=("up",), input_schema=AnalysisIn, inputs={"mode": "{mode}", "score": "{score}"})
    out = _run([up, down])

    assert out["down"].status == "ok"
    assert seen["obj"].mode == "deep"
    assert seen["obj"].score == 7 and isinstance(seen["obj"].score, int)


# --- declarative form: name in the FlowDef, class in the registry ------------


def test_flowdef_resolves_input_schema_by_name():
    reg = FlowRegistry()
    reg.schema("TriageIn")(TriageIn)
    flow = FlowDef(name="f", nodes=[NodeDef(name="n", agent="a", inputs={"ticket": "x"}, input_schema="TriageIn")])
    assert compile_flow(flow, reg)[0].input_schema is TriageIn


def test_flowdef_unknown_input_schema_raises():
    flow = FlowDef(name="f", nodes=[NodeDef(name="n", agent="a", input_schema="Nope")])
    with pytest.raises(ValueError, match="unknown input_schema"):
        compile_flow(flow, FlowRegistry())


def test_flowdef_with_input_schema_round_trips_as_data():
    """The NAME travels in the serialized flow; the class stays in code."""
    flow = FlowDef(name="f", nodes=[NodeDef(name="n", agent="a", inputs={"ticket": "x"}, input_schema="TriageIn")])
    payload = flow.model_dump_json()
    assert json.loads(payload)["nodes"][0]["input_schema"] == "TriageIn"
    assert FlowDef.model_validate_json(payload) == flow
