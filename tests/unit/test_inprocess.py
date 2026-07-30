"""Unit tests for the in-process execution path (InProcessExecutor).

Proves the AgentExecutor seam with a MOCK "PydanticAI-like" agent: a plain Python
callable that takes the neutral AgentInvocation and returns a typed pydantic
model — no subprocess, no control sidecar. The same node contract (typed
result_obj surfaced to the gate as ctx.obj) holds as for the subprocess path.
"""

from pathlib import Path

import pytest
from pydantic import BaseModel

from agent_flow.core.agent_runtime import AgentResult
from agent_flow.engine import interpret
from agent_flow.gates import Continue
from agent_flow.node_builder import agent_node
from agent_flow.protocol import PydanticSchema
from agent_flow.registry import FlowRegistry
from agent_flow.runners import AgentInvocation
from agent_flow.runners.inprocess import InProcessExecutor, adapt_result


class Classification(BaseModel):
    label: str
    confidence: float


def _inv(**kw) -> AgentInvocation:
    kw.setdefault("agent", "classifier")
    kw.setdefault("prompt", "PRODUCT_KEY: acme")
    kw.setdefault("run_dir", Path("/tmp/run"))
    return AgentInvocation(**kw)


# --- adapt_result: the three accepted return shapes -------------------------


def test_adapt_result_passthrough_agentresult():
    ar = AgentResult(agent="a", exit_code=0, duration_s=1.0, control={"status": "ok"})
    assert adapt_result(ar, _inv()) is ar  # returned as-is


def test_adapt_result_pydantic_model():
    inv = _inv(result_schema=PydanticSchema(Classification))
    out = adapt_result(Classification(label="bug", confidence=0.9), inv)
    assert out.control["status"] == "ok"
    assert out.control["result"] == {"label": "bug", "confidence": 0.9}
    assert isinstance(out.result_obj, Classification)
    assert out.result_obj.label == "bug"
    assert out.result_valid is True


def test_adapt_result_mapping_payload():
    out = adapt_result({"label": "feature", "confidence": 0.5}, _inv())
    assert out.control["status"] == "ok"
    assert out.control["result"]["label"] == "feature"


def test_adapt_result_rejects_unsupported():
    import pytest

    with pytest.raises(TypeError, match="unsupported type"):
        adapt_result(42, _inv())


# --- InProcessExecutor ------------------------------------------------------


@pytest.mark.anyio
async def test_inprocess_executor_calls_impl_and_adapts(tmp_path):
    def impl(inv):
        # A mock PydanticAI-like agent: reads the (composed) prompt, returns typed.
        assert "PRODUCT_KEY" in inv.prompt
        return Classification(label="ok", confidence=1.0)

    ex = InProcessExecutor(impl, name="pydantic")
    res = await ex.run(_inv(run_dir=tmp_path, result_schema=PydanticSchema(Classification)))
    assert isinstance(res.result_obj, Classification)
    assert res.completion == "completed"
    assert res.duration_s >= 0.0
    assert res.runtime == "pydantic"  # the executor stamps its own name


@pytest.mark.anyio
async def test_inprocess_executor_supports_async_impl(tmp_path):
    # An async impl (the PydanticAI shape: `await agent.run(...)`) is awaited
    # inline on the loop — the payoff of the async-first migration.
    async def impl(inv):
        return Classification(label="async", confidence=0.99)

    res = await InProcessExecutor(impl, name="pydantic-ai").run(_inv(run_dir=tmp_path, result_schema=PydanticSchema(Classification)))
    assert isinstance(res.result_obj, Classification)
    assert res.result_obj.label == "async"


@pytest.mark.anyio
async def test_inprocess_executor_default_runtime_label(tmp_path):
    # The default in-process runtime label is "inproc".
    res = await InProcessExecutor(lambda inv: {"status": "ok"}).run(_inv(run_dir=tmp_path))
    assert res.runtime == "inproc"


@pytest.mark.anyio
async def test_inprocess_executor_stamps_runtime_over_impl_result(tmp_path):
    # An impl returning a bare AgentResult (own runtime) does NOT override the
    # executor's authoritative label — the executor stamps it.
    def impl(inv):
        return AgentResult(agent=inv.agent, exit_code=0, duration_s=0.5, runtime="bogus", control={"status": "ok"})

    res = await InProcessExecutor(impl).run(_inv(run_dir=tmp_path))
    assert res.runtime == "inproc"


@pytest.mark.anyio
async def test_inprocess_executor_writes_no_sidecar(tmp_path):
    # The whole point: no control sidecar on disk for an in-process run.
    def impl(inv):
        return {"status": "ok"}

    await InProcessExecutor(impl).run(_inv(run_dir=tmp_path))
    assert list(tmp_path.iterdir()) == []  # nothing written


# --- end-to-end through a node + gate ---------------------------------------


@pytest.mark.anyio
async def test_agent_node_impl_runs_in_process_and_gate_reads_typed_obj(tmp_path):
    def classify(inv):
        return Classification(label="incident", confidence=0.8)

    seen = {}

    def gate(ctx):
        seen["obj"] = ctx.obj  # the validated typed object, in-process
        return Continue()

    node = agent_node(
        "classify",
        "classifier",
        inputs={"PRODUCT_KEY": "acme"},
        result_schema=PydanticSchema(Classification),
        gate=gate,
        impl=classify,
    )
    await interpret(node, run_dir=tmp_path, params={}, on_error=lambda n, e: "degraded")
    assert isinstance(seen["obj"], Classification)
    assert seen["obj"].label == "incident"
    # No sidecar for the in-process node.
    assert not any(p.name.endswith(".control.json") for p in tmp_path.iterdir())


@pytest.mark.anyio
async def test_agent_node_impl_content_failure_surfaces_to_gate(tmp_path):
    # An impl signals a content verdict by returning a non-ok AgentResult; the
    # agent-node surfaces it to the gate exactly like a sidecar status.
    def impl(inv):
        return AgentResult(agent=inv.agent, exit_code=0, duration_s=0.0, control={"status": "needs_rerun", "reason": "thin"})

    seen = {}

    def gate(ctx):
        seen["status"] = ctx.result["status"]
        return Continue()

    node = agent_node("n", "a", inputs={"K": "v"}, gate=gate, impl=impl)
    await interpret(node, run_dir=tmp_path, params={}, on_error=lambda n, e: "degraded")
    assert seen["status"] == "needs_rerun"


# --- FlowDef impl_ref + registry.agent_impl ---------------------------------


@pytest.mark.anyio
async def test_registry_agent_impl_and_flowdef_impl_ref(tmp_path):
    from agent_flow.flowdef import FlowDef, NodeDef, compile_flow

    reg = FlowRegistry()

    @reg.agent_impl("classify")
    def classify(inv):
        return Classification(label="from-registry", confidence=0.7)

    reg.schema("Classification")(PydanticSchema(Classification))

    flow = FlowDef(name="f", nodes=[NodeDef(name="c", agent="classifier", impl_ref="classify", result_schema="Classification")])
    nodes = compile_flow(flow, reg)
    seen = {}

    def _gate(ctx):
        seen["obj"] = ctx.obj
        return Continue()

    # attach a gate to read the result
    node = nodes[0]
    from dataclasses import replace as dc_replace

    node = dc_replace(node, gate=_gate)
    await interpret(node, run_dir=tmp_path, params={}, on_error=lambda n, e: "degraded", registry=reg)
    assert isinstance(seen["obj"], Classification)
    assert seen["obj"].label == "from-registry"


def test_flowdef_unknown_impl_ref_raises():
    from agent_flow.flowdef import FlowDef, NodeDef, compile_flow

    flow = FlowDef(name="f", nodes=[NodeDef(name="c", agent="a", impl_ref="missing")])
    import pytest

    with pytest.raises(ValueError, match="unknown agent impl"):
        compile_flow(flow, FlowRegistry())
