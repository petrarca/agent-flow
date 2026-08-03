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


def test_context_path_templates_work_order_inputs(tmp_path):
    # A path may template the WORK-ORDER inputs (the same keys input() exposes,
    # e.g. PRODUCT_KEY) — not only {run_dir}/params. Regression: these used to
    # be absent from the path namespace, so a path built from {PRODUCT_KEY}
    # silently resolved to a literal "{PRODUCT_KEY}/..." file.
    work_order = {"PRODUCT_REPOS_ROOT": str(tmp_path), "PRODUCT_KEY": "acme"}
    ctx = MockAgentContext(_inv(tmp_path), work_order, {"run_dir": str(tmp_path)})
    p = ctx.write_file("{PRODUCT_REPOS_ROOT}/{PRODUCT_KEY}/output/report.md", "# r")
    assert p == tmp_path / "acme" / "output" / "report.md"
    assert ctx.read_file("{PRODUCT_REPOS_ROOT}/{PRODUCT_KEY}/output/report.md") == "# r"


def test_context_path_templates_params(tmp_path):
    # Lowercase run params resolve too (precedence: inputs > params > run_dir).
    ctx = MockAgentContext(_inv(tmp_path), {}, {"run_dir": str(tmp_path), "product_key": "acme"})
    p = ctx.write_file("{run_dir}/{product_key}.md", "x")
    assert p == tmp_path / "acme.md"


def test_context_path_strict_raises_on_unknown_key(tmp_path):
    # A path MUST fully resolve: an unknown {placeholder} raises rather than
    # silently writing to a literal "{missing}/..." path (a half-resolved path is
    # never intended). Turns a typo'd key into a loud error at write time.
    ctx = MockAgentContext(_inv(tmp_path), {}, {"run_dir": str(tmp_path)})
    with pytest.raises(KeyError):
        ctx.write_file("{run_dir}/{missing_key}.md", "x")


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
    # Generic pass-through: the executor is unopinionated about the FIELD'S
    # shape (that is protocol.parse_rerun's concern, exercised in test_rerun.py)
    # — it must simply not reshape whatever the mock returned.
    ex = MockExecutor(lambda inv, ctx: {"status": "verified", "rerun_required": {"target": "a"}})
    res = await ex.run(_inv(tmp_path))
    assert res.control["rerun_required"] == {"target": "a"}


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


def test_registry_mock_agents_list_and_clear():
    r = FlowRegistry()
    r.mock_agent("a")(lambda inv, ctx: None)
    r.mock_agent("b")(lambda inv, ctx: None)
    assert r.mock_agents() == ("a", "b")  # registration order
    r.clear_mock_agents()
    assert r.mock_agents() == ()
    assert r.has_mock_agent("a") is False


def test_clear_mock_agents_leaves_other_registrations(tmp_path):
    # clear_mock_agents must NOT touch gates/exports/schemas/impls.
    r = FlowRegistry()

    @r.gate("g")
    def _g(ctx):
        from agent_flow.gates import Continue

        return Continue()

    @r.mock_agent("a")
    def _a(inv, ctx):
        return None

    r.clear_mock_agents()
    assert r.has_mock_agent("a") is False
    assert r.has_gate("g") is True  # gate survives


# --- mode routing: node_builder picks MockExecutor only when mode is on -----


async def _run_node(node, tmp_path, registry=None, **params):
    from agent_flow.flow_types import RunContext

    # The registry is run-scoped: build_flow puts it on the RunContext. A Tier-2
    # caller driving a node directly supplies it the same way.
    ctx = RunContext(node=node, run_dir=tmp_path, cycles=0, params=params, registry=registry)
    return await node.run(ctx)


@pytest.mark.anyio
async def test_mode_on_with_mock_agent_routes_to_mock(tmp_path):
    from agent_flow.node_builder import agent_node

    def stub(inv, ctx):
        ctx.write_file("{run_dir}/r.md", "x")
        return {"status": "ok", "result": {"hit": "mock"}}

    r = FlowRegistry()
    r.mock_agent("analyst")(stub)
    node = agent_node("n", "analyst", inputs={"REPORT": "{run_dir}/r.md"})
    out = await _run_node(node, tmp_path, registry=r, mock_agents=True)
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
    node = agent_node("n", "analyst", inputs={"R": "{run_dir}/r.md"})
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


# --- in-memory filesystem (UPath) for the mock path -------------------------


def test_context_resolves_memory_url_to_upath(tmp_path):
    # A memory:// run_dir makes ctx.write_file land in the in-memory FS, not disk.
    from upath import UPath

    ctx = MockAgentContext(_inv(tmp_path), {}, {"run_dir": "memory://ctx-run/output"})
    p = ctx.write_file("{run_dir}/out.md", "# hi")
    assert isinstance(p, UPath) and p.protocol == "memory"
    assert p.read_text() == "# hi"
    assert ctx.read_file("{run_dir}/out.md") == "# hi"
    # nothing landed on the real disk
    assert not (tmp_path / "out.md").exists()


def _mem_flow():
    from agent_flow import FlowDef, NodeDef

    r = FlowRegistry()

    @r.mock_agent("analyst")
    def analyst(inv, ctx):
        # write an artifact ANCHORED outside run_dir (a memory:// anchor param)
        ctx.write_file("{root}/report.md", "# report\n\nbody")
        return {"status": "ok", "result": {"ok": True}}

    flow = FlowDef(
        name="mem",
        nodes=[
            NodeDef(
                name="analyst",
                agent="analyst",
                inputs={"root": "{root}"},
                gate="require_file",
                gate_args={"path": "{root}/report.md"},
            )
        ],
    )
    return flow, r


def test_auto_memory_run_dir_for_mock_run_no_disk(tmp_path):
    # mock_agents=True + no run_dir -> a hermetic memory:// run; the artifact,
    # its sidecar, and the require_file check all resolve in memory. This is the
    # unit-test form of a mock flow: no tmp_path, no disk.
    from agent_flow import run_flow

    flow, r = _mem_flow()
    out = run_flow(flow, registry=r, mock_agents=True, root="memory://auto-run/products")
    assert out["analyst"].status == "ok"


def test_explicit_local_run_dir_under_mock_writes_disk(tmp_path):
    # The escape hatch: an explicit LOCAL run_dir keeps a mock run on disk.
    from agent_flow import FlowDef, NodeDef, run_flow

    r = FlowRegistry()

    @r.mock_agent("analyst")
    def analyst(inv, ctx):
        ctx.write_file("{run_dir}/report.md", "# report")
        return {"status": "ok", "result": {"ok": True}}

    flow = FlowDef(
        name="disk",
        nodes=[NodeDef(name="analyst", agent="analyst", gate="require_file", gate_args={"path": "{run_dir}/report.md"})],
    )
    out = run_flow(flow, registry=r, mock_agents=True, run_dir=str(tmp_path))
    assert out["analyst"].status == "ok"
    assert (tmp_path / "report.md").exists()  # really on disk


def test_memory_run_dir_is_rejected_by_the_subprocess_executor(tmp_path):
    # The in-memory FS is for the mock/in-process path only: a real subprocess
    # writes real disk and has no view of it. A memory:// run_dir on a REAL run is
    # an actionable error, not a silently bogus local path ("memory:/…").
    from upath import UPath

    from agent_flow.runners import AgentInvocation
    from agent_flow.runners.subprocess_exec import SubprocessExecutor

    class _Runner:
        name = "stub"

    ex = SubprocessExecutor(_Runner())
    inv = AgentInvocation(agent="a", prompt="p", run_dir=UPath("memory://real-run/out"), node="n")
    with pytest.raises(ValueError, match="not a local path"):
        ex._resolve_control_file(inv, None)

    # a local run_dir resolves normally
    local = AgentInvocation(agent="a", prompt="p", run_dir=tmp_path, node="n")
    assert ex._resolve_control_file(local, None) == tmp_path / "n.control.json"


def test_two_memory_mock_runs_are_isolated():
    # Distinct memory:// netloc per run -> distinct subtrees, no cross-run bleed.
    from upath import UPath

    from agent_flow import run_flow

    flow, r = _mem_flow()
    run_flow(flow, registry=r, mock_agents=True, root="memory://iso-1/products")
    # run-1 wrote its artifact; a DIFFERENT netloc must not see it.
    assert (UPath("memory://iso-1/products/report.md")).exists()
    assert not (UPath("memory://iso-2/products/report.md")).exists()  # untouched netloc is empty
