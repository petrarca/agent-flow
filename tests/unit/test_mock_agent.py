"""Unit tests for the mock_agent path (MockExecutor + MockAgentContext).

Proves the --mock-agents substitution mode: a registered mock_agent behaviour
runs via MockExecutor (in-process, no subprocess), reads STRUCTURED inputs and
writes files via the ctx tools, returns a control envelope, and the executor
writes a real sidecar + assembles the AgentResult (with schema validation).
"""

import json

import pytest

from agent_flow.registry import FlowRegistry
from agent_flow.runners import AgentInvocation, MockExecutor
from agent_flow.runners.mock_exec import MockAgentContext


def _inv(tmp_path, **kw) -> AgentInvocation:
    kw.setdefault("agent", "analyst")
    kw.setdefault("prompt", "PRODUCT_KEY: acme")
    kw.setdefault("run_dir", tmp_path)
    kw.setdefault("node", "n")
    return AgentInvocation(**kw)


# --- MockAgentContext tools -------------------------------------------------


def test_context_input_reads_structured_work_order(tmp_path):
    ctx = MockAgentContext(_inv(tmp_path), {"PRODUCT_KEY": "acme"}, {})
    assert ctx.input("PRODUCT_KEY") == "acme"
    assert ctx.input("MISSING", "d") == "d"
    assert ctx.input("MISSING") is None


def test_context_write_and_read_file_templated(tmp_path):
    tmpl = {"run_dir": str(tmp_path)}
    ctx = MockAgentContext(_inv(tmp_path), {}, tmpl)
    p = ctx.write_file("{run_dir}/out.md", "# hi")
    assert p == tmp_path / "out.md"
    assert p.read_text() == "# hi"
    assert ctx.read_file("{run_dir}/out.md") == "# hi"


# --- MockExecutor: envelope handling + sidecar + assembly -------------------


@pytest.mark.anyio
async def test_executor_writes_sidecar_and_assembles(tmp_path):
    def behaviour(inv, ctx):
        ctx.write_file("{run_dir}/r.md", "body")
        return {"status": "ok", "result": {"languages": ["Python"]}}

    ex = MockExecutor(behaviour, tmpl={"run_dir": str(tmp_path)})
    res = await ex.run(_inv(tmp_path))
    assert res.control["status"] == "ok"
    assert res.control["result"] == {"languages": ["Python"]}
    assert res.runtime == "mock"  # the executor stamps its runtime label
    # sidecar written to disk (MockRuntime surrounding), same default path.
    sidecar = tmp_path / "n.control.json"
    assert json.loads(sidecar.read_text())["result"] == {"languages": ["Python"]}
    assert (tmp_path / "r.md").read_text() == "body"


@pytest.mark.anyio
async def test_executor_none_return_is_bare_ok(tmp_path):
    ex = MockExecutor(lambda inv, ctx: None)
    res = await ex.run(_inv(tmp_path))
    assert res.control["status"] == "ok"
    assert res.control["agent"] == "analyst"


@pytest.mark.anyio
async def test_executor_defaults_status_ok(tmp_path):
    ex = MockExecutor(lambda inv, ctx: {"result": {"x": 1}})
    res = await ex.run(_inv(tmp_path))
    assert res.control["status"] == "ok"


@pytest.mark.anyio
async def test_executor_preserves_rerun_required(tmp_path):
    ex = MockExecutor(lambda inv, ctx: {"status": "verified", "rerun_required": ["a"]})
    res = await ex.run(_inv(tmp_path))
    assert res.control["rerun_required"] == ["a"]


@pytest.mark.anyio
async def test_executor_rejects_non_dict_return(tmp_path):
    ex = MockExecutor(lambda inv, ctx: ["not", "a", "dict"])
    with pytest.raises(TypeError):
        await ex.run(_inv(tmp_path))


def test_executor_rejects_non_callable():
    with pytest.raises(TypeError):
        MockExecutor("nope")


@pytest.mark.anyio
async def test_executor_validates_result_schema(tmp_path):
    schema = {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]}
    ok = MockExecutor(lambda inv, ctx: {"status": "ok", "result": {"n": 1}})
    res = await ok.run(_inv(tmp_path, result_schema=schema))
    assert res.result_valid is True and res.result_errors == ()

    bad = MockExecutor(lambda inv, ctx: {"status": "ok", "result": {"n": "x"}})
    res = await bad.run(_inv(tmp_path, result_schema=schema))
    assert res.result_valid is False and res.result_errors


# --- FlowRegistry.mock_agent trio -------------------------------------------


def test_registry_mock_agent_registration():
    r = FlowRegistry()
    assert r.has_mock_agent("a") is False

    @r.mock_agent("a")
    def _b(inv, ctx):
        return {"status": "ok"}

    assert r.has_mock_agent("a") is True
    assert r.get_mock_agent("a") is _b


def test_registry_get_unknown_mock_agent_raises():
    with pytest.raises(ValueError):
        FlowRegistry().get_mock_agent("nope")


# --- mode routing: node_builder picks MockExecutor only when mode is on -----


async def _run_node(node, tmp_path, **params):
    from agent_flow.engine import RunContext

    ctx = RunContext(node=node, run_dir=tmp_path, cycles=0, params=params)
    return await node.run(ctx)


@pytest.mark.anyio
async def test_mode_on_with_mock_agent_routes_to_mock(tmp_path):
    from agent_flow.node_builder import agent_node

    def stub(inv, ctx):
        ctx.write_file("{run_dir}/r.md", "x")
        return {"status": "ok", "result": {"hit": "mock"}}

    r = FlowRegistry()
    r.mock_agent("analyst")(stub)
    node = agent_node("n", "analyst", inputs={"REPORT": "{run_dir}/r.md"}, registry=r)
    out = await _run_node(node, tmp_path, mock_agents=True)
    assert out["result"] == {"hit": "mock"}
    assert (tmp_path / "r.md").exists()


@pytest.mark.anyio
async def test_mode_off_ignores_mock_agent(tmp_path):
    # mode OFF + no impl -> normal subprocess path (get_executor("opencode")).
    # Mock behaviour not called; normal path attempted with a bogus runtime -> ValueError.
    from agent_flow.node_builder import agent_node

    called = {"mock": False}

    def stub(inv, ctx):
        called["mock"] = True
        return {"status": "ok"}

    r = FlowRegistry()
    r.mock_agent("analyst")(stub)
    node = agent_node("n", "analyst", inputs={"R": "{run_dir}/r.md"}, registry=r)
    with pytest.raises(ValueError):  # unknown runtime -> get_executor raises
        await _run_node(node, tmp_path, mock_agents=False, runtime="does-not-exist")
    assert called["mock"] is False


@pytest.mark.anyio
async def test_partial_mock_fallback_for_unmocked_node(tmp_path):
    # mode ON but this node has NO mock_agent -> falls through to normal executor.
    from agent_flow.node_builder import agent_node

    node = agent_node("n", "analyst", inputs={"R": "{run_dir}/r.md"})  # no mock_agent
    with pytest.raises(ValueError):  # unknown runtime -> normal path attempted
        await _run_node(node, tmp_path, mock_agents=True, runtime="does-not-exist")


def test_declarative_compile_resolves_mock_agent(tmp_path):
    from agent_flow import FlowDef, NodeDef, run_flow

    r = FlowRegistry()

    @r.mock_agent("analyst")
    def stub(inv, ctx):
        return {"status": "ok", "result": {"ok": True}}

    flow = FlowDef(name="t", nodes=[NodeDef(name="n", agent="analyst")])
    res = run_flow(flow, registry=r, run_dir=str(tmp_path), mock_agents=True)
    assert res["n"].status == "ok"
