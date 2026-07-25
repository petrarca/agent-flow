"""Unit tests for the batteries layer: agent_node, ready gates, cross-node jump-back."""

from agent_flow.batteries import agent_node, build_work_order, control_path
from agent_flow.engine import Node, NodeOutcome, _walk, plan_groups
from agent_flow.gates import Continue, GateContext, GoTo, Restart, require_file, rerun_on_signal


class _L:
    def info(self, *a):
        pass

    def warning(self, *a):
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
    assert "PRODUCT_KEY: acme" in order
    assert "REPORT: /w/r.md" in order


def test_build_work_order_leaves_unknown_placeholders():
    order = build_work_order({"X": "{missing}"}, {})
    assert order == "X: {missing}"  # graceful, no KeyError


def test_control_path_is_per_node():
    assert control_path("domain") == "domain.control.json"


def test_gate_receives_validated_result_obj(tmp_path, monkeypatch):
    # A gate must be able to decide on the VALIDATED typed object, not just the
    # raw dict: agent_node hands it through as ctx.result["_result_obj"].
    from pathlib import Path

    from pydantic import BaseModel

    from agent_flow.core.schema_pydantic import PydanticSchema
    from agent_flow.engine import interpret

    class R(BaseModel):
        summary: str
        languages: list[str]

    class _Result:
        control = {"status": "ok"}
        tokens = cost = events = 0
        duration_s = 0.0
        completion = "sidecar"
        result_valid = True
        result_obj = R(summary="s", languages=["Python", "Go"])
        result_errors = ()

    monkeypatch.setattr("agent_flow.batteries.run_agent", lambda **kw: _Result())

    seen = {}

    def gate(ctx):
        seen["obj"] = ctx.result["_result_obj"]
        return Continue()

    node = agent_node("n", "agent-x", inputs={"K": "v"}, result_schema=PydanticSchema(R), gate=gate)
    interpret(node, run_dir=Path(tmp_path), params={}, on_error=lambda n, e: "degraded")

    assert isinstance(seen["obj"], R)
    assert seen["obj"].languages == ["Python", "Go"]  # gate can read typed fields


# --- ready-made gates -------------------------------------------------------


def test_require_file_continue_when_present(tmp_path):
    (tmp_path / "r.md").write_text("x")
    node = Node("n", run=lambda c: None)
    d = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), relpath="r.md")
    assert isinstance(d, Continue)


def test_require_file_restart_when_missing(tmp_path):
    node = Node("n", run=lambda c: None)
    d = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), relpath="r.md")
    assert isinstance(d, Restart)


def test_require_file_templates_against_run_params(tmp_path):
    # {name} resolves against ctx.params — the SAME run-time values the node's
    # run() saw — not a nonexistent node.inputs (a Node carries no such field).
    (tmp_path / "acme-report.md").write_text("x")
    node = Node("n", run=lambda c: None)
    ctx = GateContext(result={}, node=node, run_dir=tmp_path, cycles=0, params={"product_key": "acme"})
    d = require_file(ctx, relpath="{product_key}-report.md")
    assert isinstance(d, Continue)


def test_require_file_missing_placeholder_is_left_literal(tmp_path):
    # A template referencing an unknown param must not raise — it degrades to
    # the literal string, which then correctly reports as missing.
    node = Node("n", run=lambda c: None)
    d = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), relpath="{unknown}-report.md")
    assert isinstance(d, Restart)


def test_require_file_run_dir_template_matches_bare_relpath(tmp_path):
    # {run_dir} resolves in a gate too, and "{run_dir}/x.md" points to the SAME
    # file as the bare "x.md" (Path join drops the run_dir prefix when the RHS is
    # already absolute). Both must Continue when the file exists.
    (tmp_path / "hello.md").write_text("x")
    node = Node("n", run=lambda c: None)
    bare = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), relpath="hello.md")
    prefixed = require_file(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), relpath="{run_dir}/hello.md")
    assert isinstance(bare, Continue)
    assert isinstance(prefixed, Continue)


def test_rerun_on_signal_goto_when_flagged(tmp_path):
    (tmp_path / "verify.control.json").write_text('{"status":"verified","rerun_required":["analyst"]}')
    node = Node("verify", run=lambda c: None)
    d = rerun_on_signal(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), target="analyst")
    assert isinstance(d, GoTo)
    assert d.node == "analyst"


def test_rerun_on_signal_continue_when_clean(tmp_path):
    (tmp_path / "verify.control.json").write_text('{"status":"verified"}')
    node = Node("verify", run=lambda c: None)
    d = rerun_on_signal(GateContext(result={}, node=node, run_dir=tmp_path, cycles=0), target="analyst")
    assert isinstance(d, Continue)


# --- cross-node jump-back (the walker) --------------------------------------


def _plan(nodes):
    planned = plan_groups(nodes)
    return planned, {k: i for i, (k, _) in enumerate(planned)}, {n.name: (n.parallel_group or n.name) for n in nodes}


def test_walk_cross_node_jump_back_bounded():
    a = Node("A", run=lambda c: None, max_cycles=1)
    b = Node("B", run=lambda c: None, depends_on=("A",), max_cycles=1)
    planned, gi, ng = _plan([a, b])
    calls: list[str] = []
    state = {"jumped": False}

    def run_group(group):
        out = {}
        for n in group:
            calls.append(n.name)
            if n.name == "B" and not state["jumped"]:
                state["jumped"] = True
                out[n.name] = NodeOutcome(status="ok", goto="A")
            else:
                out[n.name] = NodeOutcome(status="ok")
        return out

    _walk(planned, run_group=run_group, group_index=gi, node_group=ng, by_name={"A": a, "B": b}, logger=_L())
    assert calls == ["A", "B", "A", "B"]  # one jump-back, then done


def test_walk_jump_back_respects_max_cycles():
    a = Node("A", run=lambda c: None, max_cycles=1)
    b = Node("B", run=lambda c: None, depends_on=("A",))
    planned, gi, ng = _plan([a, b])
    calls: list[str] = []

    def run_group(group):  # B ALWAYS asks to jump back to A
        out = {}
        for n in group:
            calls.append(n.name)
            out[n.name] = NodeOutcome("ok", goto="A") if n.name == "B" else NodeOutcome("ok")
        return out

    _walk(planned, run_group=run_group, group_index=gi, node_group=ng, by_name={"A": a, "B": b}, logger=_L())
    # A runs twice at most (max_cycles=1), so it terminates rather than looping.
    assert calls.count("A") == 2


def test_walk_ignores_forward_goto():
    a = Node("A", run=lambda c: None)
    b = Node("B", run=lambda c: None, depends_on=("A",), max_cycles=1)
    planned, gi, ng = _plan([a, b])
    calls: list[str] = []

    def run_group(group):
        out = {}
        for n in group:
            calls.append(n.name)
            out[n.name] = NodeOutcome("ok", goto="B") if n.name == "A" else NodeOutcome("ok")  # forward goto — ignored
        return out

    _walk(planned, run_group=run_group, group_index=gi, node_group=ng, by_name={"A": a, "B": b}, logger=_L())
    assert calls == ["A", "B"]  # forward goto ignored, no rewind
