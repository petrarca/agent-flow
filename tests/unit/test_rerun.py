"""The agent-requested re-run: grant, protocol, preamble, and the honored jump.

`rerun_targets` on a node is the whole opt-in — it grants the lever, names the
legal targets in the agent's preamble, and authorizes the jump. No gate is
involved (a gate remains the override).
"""

import anyio
import pytest

from agent_flow import Node, NodeBlocked, build_flow
from agent_flow.protocol import RerunRequest, RerunSpec, build_control_preamble, parse_rerun
from agent_flow.protocol.rerun import RerunTarget

ONE = RerunSpec.of("domain")
MANY = RerunSpec((RerunTarget("tech-stack"), RerunTarget("analysis", ("security", "domain")), RerunTarget("architecture")))


# --- parsing the request ------------------------------------------------------


def test_true_resolves_to_the_sole_granted_target():
    # With one grant there is nothing to choose, so `true` is unambiguous.
    assert parse_rerun({"rerun_required": True}, ONE) == RerunRequest(target="domain", instruction="")


def test_bare_string_names_the_target():
    req = parse_rerun({"rerun_required": "domain"}, ONE)
    assert req.target == "domain" and req.instruction == ""


def test_object_form_carries_the_instruction():
    req = parse_rerun({"rerun_required": {"target": "domain", "instruction": "  recompute  "}}, ONE)
    assert req.target == "domain" and req.instruction == "recompute"


def test_target_may_be_omitted_when_only_one_is_granted():
    req = parse_rerun({"rerun_required": {"instruction": "fix it"}}, ONE)
    assert req.target == "domain" and req.instruction == "fix it"


def test_true_is_ambiguous_when_several_granted():
    # The agent had a choice and did not make it -> no request.
    assert parse_rerun({"rerun_required": True}, MANY) is None


def test_absent_false_or_empty_is_no_request():
    assert parse_rerun({"status": "ok"}, ONE) is None
    assert parse_rerun({"rerun_required": False}, ONE) is None
    assert parse_rerun({"rerun_required": "  "}, ONE) is None


def test_no_grant_means_the_field_is_ignored():
    # The lever is granted per node; without one the request means nothing.
    assert parse_rerun({"rerun_required": True}, None) is None


def test_non_dict_control_is_no_request():
    assert parse_rerun(None, ONE) is None
    assert parse_rerun("not a dict", ONE) is None


# --- the preamble -------------------------------------------------------------


def test_no_grant_means_no_rerun_text_at_all():
    # The common case: an agent that cannot re-run is told nothing about it.
    assert "rerun_required" not in build_control_preamble("a", "/run/c.json")


def test_granted_preamble_names_the_sole_target():
    # Both accepted values are shown, and shown TOGETHER — split apart, the first
    # reads as the only legal one (an agent reported exactly that).
    text = build_control_preamble("a", "/run/c.json", None, ONE)
    assert "always means one step: domain" in text
    assert "There is no target key" in text
    assert "    true\n" in text
    assert '{ "instruction": "what domain must fix or redo" }' in text


def test_granted_preamble_says_the_field_is_top_level():
    # A bare `"rerun_required": true` fragment does not say WHERE it goes, and a
    # request nested under `result` is read by nobody — so the block must place
    # it explicitly (an agent flagged exactly this ambiguity).
    for spec in (ONE, MANY):
        text = build_control_preamble("a", "/run/c.json", None, spec)
        assert "TOP LEVEL" in text
        assert 'never inside "result"' in text


def test_granted_preamble_lists_targets_and_expands_a_group():
    # A bare group name is opaque; the block spells out what it covers.
    text = build_control_preamble("a", "/run/c.json", None, MANY)
    assert "- tech-stack" in text
    assert "- analysis  (runs: security, domain)" in text
    assert '"target": "<step>"' in text


# --- the honored jump ---------------------------------------------------------


def _recorder(name, calls, seen):
    def run(ctx):
        calls.append(name)
        if ctx.one_time_instruction:
            seen[name] = ctx.one_time_instruction
        return {"status": "ok"}

    return run


def test_request_jumps_back_and_delivers_the_instruction():
    calls, seen = [], {}

    def verify(ctx):
        calls.append("verify")
        if calls.count("verify") == 1:
            return {"status": "verified", "rerun_required": {"instruction": "add the Deployment section"}}
        return {"status": "verified"}

    nodes = [
        Node(name="analyst", run=_recorder("analyst", calls, seen)),
        Node(name="verify", run=verify, depends_on=("analyst",), rerun_targets=("analyst",), max_cycles=2),
    ]
    anyio.run(lambda: build_flow(nodes, name="t")(run_dir=""))
    assert calls == ["analyst", "verify", "analyst", "verify"]
    assert seen["analyst"] == "add the Deployment section"


def test_a_group_target_reruns_every_member_and_broadcasts():
    # Choosing the GROUP is the claim that the reason applies to the whole wave.
    calls, seen = [], {}

    def check(ctx):
        calls.append("check")
        if calls.count("check") == 1:
            return {"status": "ok", "rerun_required": {"target": "analysis", "instruction": "redo"}}
        return {"status": "ok"}

    nodes = [
        Node(name="a", run=_recorder("a", calls, seen), parallel_group="analysis"),
        Node(name="b", run=_recorder("b", calls, seen), parallel_group="analysis"),
        Node(name="check", run=check, depends_on=("a", "b"), rerun_targets=("analysis",), max_cycles=2),
    ]
    anyio.run(lambda: build_flow(nodes, name="t")(run_dir=""))
    assert calls == ["a", "b", "check", "a", "b", "check"]
    assert seen == {"a": "redo", "b": "redo"}


def test_request_is_ignored_without_a_grant():
    calls = []

    def verify(ctx):
        calls.append("verify")
        return {"status": "verified", "rerun_required": {"target": "analyst"}}

    nodes = [
        Node(name="analyst", run=_recorder("analyst", calls, {})),
        Node(name="verify", run=verify, depends_on=("analyst",)),  # no rerun_targets
    ]
    anyio.run(lambda: build_flow(nodes, name="t")(run_dir=""))
    assert calls == ["analyst", "verify"]


def _verify_asking_rerun(calls):
    def verify(ctx):
        calls.append("verify")
        return {"status": "verified", "rerun_required": True}

    return verify


def test_a_gate_continue_lets_the_declaration_apply():
    # The declaration is the DEFAULT: an undecided gate does not suppress it.
    from agent_flow.gates import Continue

    calls = []
    nodes = [
        Node(name="analyst", run=_recorder("analyst", calls, {})),
        Node(name="verify", run=_verify_asking_rerun(calls), depends_on=("analyst",), rerun_targets=("analyst",), gate=lambda ctx: Continue()),
    ]
    anyio.run(lambda: build_flow(nodes, name="t")(run_dir=""))
    assert calls.count("analyst") == 2


def test_a_deciding_gate_overrides_the_declaration():
    # The gate is the ESCAPE HATCH: its non-Continue directive wins, so the
    # agent's request is not acted on as well (no jump-back happens here).
    from agent_flow.gates import Stop

    calls = []
    nodes = [
        Node(name="analyst", run=_recorder("analyst", calls, {})),
        Node(name="verify", run=_verify_asking_rerun(calls), depends_on=("analyst",), rerun_targets=("analyst",), gate=lambda ctx: Stop("halt")),
    ]
    with pytest.raises(NodeBlocked):
        anyio.run(lambda: build_flow(nodes, name="t")(run_dir=""))
    assert calls.count("analyst") == 1  # never re-ran: the gate decided first


def test_max_cycles_bounds_the_agent_too():
    # An agent asking forever is bounded by the TARGET's max_cycles.
    calls = []

    def verify(ctx):
        calls.append("verify")
        return {"status": "verified", "rerun_required": True}

    nodes = [
        Node(name="analyst", run=_recorder("analyst", calls, {}), max_cycles=1),
        Node(name="verify", run=verify, depends_on=("analyst",), rerun_targets=("analyst",)),
    ]
    anyio.run(lambda: build_flow(nodes, name="t")(run_dir=""))
    assert calls.count("analyst") == 2  # first run + one re-run, then exhausted


def test_an_ungranted_target_is_refused_even_if_it_is_a_valid_node():
    # The grant is an ALLOWLIST. A plausible-but-ungranted node — valid, backward,
    # unexhausted — must not steer the flow just because the DAG could honor it.
    calls = []

    def check(ctx):
        calls.append("check")
        return {"status": "ok", "rerun_required": {"target": "secret"}}

    nodes = [
        Node(name="secret", run=_recorder("secret", calls, {})),
        Node(name="allowed", run=_recorder("allowed", calls, {}), depends_on=("secret",)),
        Node(name="check", run=check, depends_on=("allowed",), rerun_targets=("allowed",), max_cycles=2),
    ]
    anyio.run(lambda: build_flow(nodes, name="t")(run_dir=""))
    assert calls == ["secret", "allowed", "check"]  # no jump at all


# --- build-time validation ----------------------------------------------------


def test_a_solo_node_target_is_not_reported_as_a_group():
    # A solo node is its own group KEY, so a naive membership test would render
    # it as a group standing for itself ("tech-stack (runs: tech-stack)").
    from agent_flow.engine.builder import _resolve_rerun_grants
    from agent_flow.engine.planner import plan_groups

    nodes = [
        Node(name="solo", run=lambda c: {}),
        Node(name="a", run=lambda c: {}, depends_on=("solo",), parallel_group="wave"),
        Node(name="b", run=lambda c: {}, depends_on=("solo",), parallel_group="wave"),
        Node(name="check", run=lambda c: {}, depends_on=("a", "b"), rerun_targets=("solo", "wave")),
    ]
    planned = plan_groups(nodes)
    gi = {k: i for i, (k, _) in enumerate(planned)}
    ng = {n.name: (n.parallel_group or n.name) for n in nodes}
    spec = _resolve_rerun_grants(nodes, planned, gi, ng)["check"]
    assert spec.targets[0] == RerunTarget("solo", ())  # a node: stands for itself
    assert spec.targets[1] == RerunTarget("wave", ("a", "b"))  # a real group


def test_unknown_target_fails_the_build():
    nodes = [Node(name="x", run=lambda c: {}, rerun_targets=("nope",))]
    with pytest.raises(ValueError, match="not a known node or parallel group"):
        build_flow(nodes, name="t")


def test_forward_target_fails_the_build():
    # A jump-back can only resume at something that already ran.
    nodes = [Node(name="x", run=lambda c: {}, rerun_targets=("later",)), Node(name="later", run=lambda c: {}, depends_on=("x",))]
    with pytest.raises(ValueError, match="does not run BEFORE it"):
        build_flow(nodes, name="t")


def test_self_target_fails_the_build():
    # A node is not BEFORE itself; re-running in place is max_cycles' job.
    nodes = [Node(name="x", run=lambda c: {}, rerun_targets=("x",))]
    with pytest.raises(ValueError, match="does not run BEFORE it"):
        build_flow(nodes, name="t")
