"""Unit tests for the agent-node layer: agent_node, ready gates, cross-node jump-back."""

import anyio
import pytest

from agent_flow.engine import Node, NodeOutcome, _walk, plan_groups
from agent_flow.gates import Continue, GateContext, GoTo, Restart, require_file, rerun_on_signal
from agent_flow.node_builder import agent_node, build_work_order, control_path


class _L:
    def info(self, *a):
        pass

    def warning(self, *a):
        pass

    def debug(self, *a):
        pass


# --- agent_node -------------------------------------------------------------


def test_agent_node_builds_a_plain_node():
    n = agent_node("tech-stack", "tech-stack-analyst", inputs={"REPORT": "{run_dir}/r.md"}, depends_on=("x",))
    assert isinstance(n, Node)
    assert n.name == "tech-stack"
    assert n.depends_on == ("x",)
    assert callable(n.run)


def test_build_work_order_templates_params():
    order = build_work_order({"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/r.md"}, {"product_key": "acme", "run_dir": "/w"})
    assert "<PRODUCT_KEY>acme</PRODUCT_KEY>" in order  # XML is the default shape
    assert "<REPORT>/w/r.md</REPORT>" in order


def test_build_work_order_leaves_unknown_placeholders():
    order = build_work_order({"X": "{missing}"}, {})
    assert order == "<X>{missing}</X>"  # graceful, no KeyError


def test_build_work_order_accepts_an_explicit_renderer():
    from agent_flow.node_builder import render_work_order_lines

    order = build_work_order({"X": "1", "Y": "2"}, {}, render=render_work_order_lines)
    assert order == "X: 1\nY: 2"


def test_xml_renderer_delimits_a_multiline_value():
    """The reason XML is the default: a line-oriented work order cannot mark
    where a multi-line value ends, so its continuation is indistinguishable from
    the next key."""
    from agent_flow.node_builder import render_work_order_lines, render_work_order_xml

    resolved = {"A": "one\ntwo", "B": "3"}
    assert render_work_order_xml(resolved) == "<A>\none\ntwo\n</A>\n<B>3</B>"
    assert render_work_order_lines(resolved) == "A: one\ntwo\nB: 3"  # ambiguous by construction


def test_control_path_is_per_node():
    assert control_path("domain") == "domain.control.json"


def test_gate_receives_validated_result_obj(tmp_path, monkeypatch):
    # A gate must be able to decide on the VALIDATED typed object, not just the
    # raw dict: agent_node hands it through as ctx.result["_result_obj"].
    from pathlib import Path

    from pydantic import BaseModel

    from agent_flow.core.agent_runtime import AgentResult
    from agent_flow.engine import interpret
    from agent_flow.protocol import PydanticSchema

    class R(BaseModel):
        summary: str
        languages: list[str]

    async def _run(inv):
        return AgentResult(
            agent=inv.agent,
            exit_code=0,
            duration_s=0.0,
            control={"status": "ok"},
            completion="completed",
            result_obj=R(summary="s", languages=["Python", "Go"]),
        )

    class _FakeExecutor:
        name = "fake"
        run = staticmethod(_run)

    monkeypatch.setattr("agent_flow.node_builder.executor_choice.get_executor", lambda _runtime, **_kw: _FakeExecutor())

    seen = {}

    def gate(ctx):
        seen["obj"] = ctx.result["_result_obj"]
        return Continue()

    node = agent_node("n", "agent-x", inputs={"K": "v"}, result_schema=PydanticSchema(R), gate=gate)
    anyio.run(lambda: interpret(node, run_dir=Path(tmp_path), params={}, on_error=lambda n, e: "degraded"))

    assert isinstance(seen["obj"], R)
    assert seen["obj"].languages == ["Python", "Go"]  # gate can read typed fields


# --- ready-made gates -------------------------------------------------------


def test_require_file_continue_when_present(tmp_path):
    (tmp_path / "r.md").write_text("x")
    node = Node("n", run=lambda c: None)
    d = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), path="r.md")
    assert isinstance(d, Continue)


def test_require_file_restart_when_missing(tmp_path):
    node = Node("n", run=lambda c: None)
    d = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), path="r.md")
    assert isinstance(d, Restart)


def test_require_file_templates_against_run_params(tmp_path):
    # {name} resolves against ctx.params — the SAME run-time values the node's
    # run() saw — not a nonexistent node.inputs (a Node carries no such field).
    (tmp_path / "acme-report.md").write_text("x")
    node = Node("n", run=lambda c: None)
    ctx = GateContext(result={}, node=node, run_dir=tmp_path, cycles=0, params={"product_key": "acme"})
    d = require_file(ctx, path="{product_key}-report.md")
    assert isinstance(d, Continue)


def test_require_file_missing_placeholder_is_left_literal(tmp_path):
    # A template referencing an unknown param must not raise — it degrades to
    # the literal string, which then correctly reports as missing.
    node = Node("n", run=lambda c: None)
    d = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), path="{unknown}-report.md")
    assert isinstance(d, Restart)


def test_require_file_run_dir_template_matches_bare_path(tmp_path):
    # {run_dir} resolves in a gate too, and "{run_dir}/x.md" points to the SAME
    # file as the bare "x.md" (Path join drops the run_dir prefix when the RHS is
    # already absolute). Both must Continue when the file exists.
    (tmp_path / "hello.md").write_text("x")
    node = Node("n", run=lambda c: None)
    bare = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), path="hello.md")
    prefixed = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), path="{run_dir}/hello.md")
    assert isinstance(bare, Continue)
    assert isinstance(prefixed, Continue)


def test_rerun_on_signal_goto_when_flagged(tmp_path):
    # The verdict is read from ctx.result (the harvested envelope), not a file.
    node = Node("verify", run=lambda c: None)
    result = {"status": "verified", "rerun_required": ["analyst"]}
    d = rerun_on_signal(GateContext(result=result, node=node, run_dir=tmp_path, cycles=0), target="analyst")
    assert isinstance(d, GoTo)
    assert d.node == "analyst"


def test_rerun_on_signal_continue_when_clean(tmp_path):
    node = Node("verify", run=lambda c: None)
    d = rerun_on_signal(GateContext(result={"status": "verified"}, node=node, run_dir=tmp_path, cycles=0), target="analyst")
    assert isinstance(d, Continue)


# --- cross-node jump-back (the walker) --------------------------------------


def _plan(nodes):
    planned = plan_groups(nodes)
    return planned, {k: i for i, (k, _) in enumerate(planned)}, {n.name: (n.parallel_group or n.name) for n in nodes}


@pytest.mark.anyio
async def test_walk_cross_node_jump_back_bounded():
    a = Node("A", run=lambda c: None, max_cycles=1)
    b = Node("B", run=lambda c: None, depends_on=("A",), max_cycles=1)
    planned, gi, ng = _plan([a, b])
    calls: list[str] = []
    state = {"jumped": False}

    async def run_group(group):
        out = {}
        for n in group:
            calls.append(n.name)
            if n.name == "B" and not state["jumped"]:
                state["jumped"] = True
                out[n.name] = NodeOutcome(status="ok", goto="A")
            else:
                out[n.name] = NodeOutcome(status="ok")
        return out

    await _walk(planned, run_group=run_group, group_index=gi, node_group=ng, by_name={"A": a, "B": b}, logger=_L())
    assert calls == ["A", "B", "A", "B"]  # one jump-back, then done


@pytest.mark.anyio
async def test_walk_jump_back_respects_max_cycles():
    a = Node("A", run=lambda c: None, max_cycles=1)
    b = Node("B", run=lambda c: None, depends_on=("A",))
    planned, gi, ng = _plan([a, b])
    calls: list[str] = []

    async def run_group(group):  # B ALWAYS asks to jump back to A
        out = {}
        for n in group:
            calls.append(n.name)
            out[n.name] = NodeOutcome("ok", goto="A") if n.name == "B" else NodeOutcome("ok")
        return out

    await _walk(planned, run_group=run_group, group_index=gi, node_group=ng, by_name={"A": a, "B": b}, logger=_L())
    # A runs twice at most (max_cycles=1), so it terminates rather than looping.
    assert calls.count("A") == 2


@pytest.mark.anyio
async def test_walk_ignores_forward_goto():
    a = Node("A", run=lambda c: None)
    b = Node("B", run=lambda c: None, depends_on=("A",), max_cycles=1)
    planned, gi, ng = _plan([a, b])
    calls: list[str] = []

    async def run_group(group):
        out = {}
        for n in group:
            calls.append(n.name)
            out[n.name] = NodeOutcome("ok", goto="B") if n.name == "A" else NodeOutcome("ok")  # forward goto — ignored
        return out

    await _walk(planned, run_group=run_group, group_index=gi, node_group=ng, by_name={"A": a, "B": b}, logger=_L())
    assert calls == ["A", "B"]  # forward goto ignored, no rewind


@pytest.mark.anyio
async def test_walk_delivers_goto_instruction_to_target():
    # A cross-node GoTo carrying an instruction lands in pending_instructions
    # keyed by the TARGET node, for run_node to hand to the target's next run.
    a = Node("A", run=lambda c: None, max_cycles=1)
    b = Node("B", run=lambda c: None, depends_on=("A",), max_cycles=1)
    planned, gi, ng = _plan([a, b])
    pending: dict[str, str] = {}
    state = {"jumped": False}

    async def run_group(group):
        out = {}
        for n in group:
            if n.name == "B" and not state["jumped"]:
                state["jumped"] = True
                out[n.name] = NodeOutcome(status="ok", goto="A", instruction="redo A")
            else:
                out[n.name] = NodeOutcome(status="ok")
        return out

    await _walk(
        planned,
        run_group=run_group,
        group_index=gi,
        node_group=ng,
        by_name={"A": a, "B": b},
        logger=_L(),
        pending_instructions=pending,
    )
    # After the run settles, A's slot has been popped (delivered); but we can at
    # least assert the instruction was routed to the target key during the jump.
    # Since this run_group does not drain `pending`, it remains recorded for A.
    assert pending.get("A") == "redo A"


# --- node-local inputs available to gates -----------------------------------


def test_gate_resolves_input_key_from_node_inputs(tmp_path):
    # A gate's path= can reference {REPORT} from the node's own inputs — no need
    # to repeat the value in gate_args. The resolved input is injected into the
    # gate result dict under _inputs.
    report = tmp_path / "report.md"
    report.write_text("content")
    node = Node("n", run=lambda c: None)
    # Simulate the result dict agent_node produces (with _inputs populated).
    result = {"status": "ok", "_inputs": {"REPORT": str(report)}}
    ctx = GateContext(result=result, obj=None, node=node, run_dir=tmp_path, cycles=0, params={}, agent_dir="")
    d = require_file(ctx, path="{REPORT}")
    assert isinstance(d, Continue)


def test_gate_node_input_missing_file_restarts(tmp_path):
    # When {REPORT} from node inputs points at a non-existent file -> Restart.
    node = Node("n", run=lambda c: None)
    result = {"status": "ok", "_inputs": {"REPORT": str(tmp_path / "missing.md")}}
    ctx = GateContext(result=result, obj=None, node=node, run_dir=tmp_path, cycles=0, params={}, agent_dir="")
    d = require_file(ctx, path="{REPORT}")
    assert isinstance(d, Restart)


def test_gate_node_input_wins_over_global_param(tmp_path):
    # Node-local input wins over a same-named global param.
    local_file = tmp_path / "local.md"
    local_file.write_text("local")
    node = Node("n", run=lambda c: None)
    result = {"status": "ok", "_inputs": {"REPORT": str(local_file)}}
    # Global param points at a non-existent file — local input must win.
    global_params = {"REPORT": str(tmp_path / "global_missing.md")}
    ctx = GateContext(result=result, obj=None, node=node, run_dir=tmp_path, cycles=0, params=global_params, agent_dir="")
    d = require_file(ctx, path="{REPORT}")
    assert isinstance(d, Continue)  # local file exists -> Continue, not Restart


def test_gate_node_inputs_do_not_pollute_global_params(tmp_path):
    # Node-local inputs must NOT flow into the shared run params.
    # Verified by checking that ctx.params is unchanged after the gate call.
    node = Node("n", run=lambda c: None)
    global_params = {"product_key": "acme"}
    result = {"status": "ok", "_inputs": {"REPORT": str(tmp_path / "r.md")}}
    ctx = GateContext(result=result, obj=None, node=node, run_dir=tmp_path, cycles=0, params=global_params, agent_dir="")
    require_file(ctx, path="{REPORT}")
    assert ctx.params == {"product_key": "acme"}  # unchanged — no REPORT leaked in
    assert "REPORT" not in ctx.params


def test_registry_can_override_the_work_order_renderer():
    """The override is a REGISTRY registration, not a build_flow/agent_node
    parameter — consumer code lives in the registry, like gates and exports, so
    the node/flow signatures do not grow a knob per presentation choice."""
    import tempfile

    import anyio

    from agent_flow import agent_node, build_flow
    from agent_flow.node_builder import render_work_order_lines
    from agent_flow.registry import FlowRegistry

    seen = {}

    async def impl(inv):
        seen[inv.node] = inv.prompt
        return {"status": "ok"}

    def _run(reg, tag):
        n = agent_node(tag, "a", impl=impl, inputs={"K": "v"}, registry=reg)
        with tempfile.TemporaryDirectory() as d:
            anyio.run(lambda: build_flow([n], name="w", registry=reg)(run_dir=d))

    _run(FlowRegistry(), "default")

    reg = FlowRegistry()
    reg.work_order(render_work_order_lines)  # opt back into the pre-0.3 shape
    _run(reg, "overridden")

    assert "<K>v</K>" in seen["default"]
    assert "K: v" in seen["overridden"]
