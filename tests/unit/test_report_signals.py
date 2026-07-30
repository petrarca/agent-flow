"""Unit tests for report_signals — produced() (file check) + rerun_targets() (envelope)."""

from pathlib import Path

from agent_flow.gates import Continue, GateContext, GoTo, rerun_on_named, rerun_on_signal
from agent_flow.gates.signals import produced, rerun_targets


class _N:
    """Minimal node stand-in (gates read node.name)."""

    def __init__(self, name):
        self.name = name


def _ctx(node_name, rerun=None):
    """GateContext whose result is the HARVESTED control envelope (no file)."""
    control: dict = {"status": "verified", "agent": "x"}
    if rerun is not None:
        control["rerun_required"] = rerun
    return GateContext(result=control, node=_N(node_name), run_dir=Path("/tmp/run"), cycles=0, params={})


def test_rerun_on_signal_fixed_target_when_signalled():
    d = rerun_on_signal(_ctx("verify", ["analyst"]), target="analyst")
    assert isinstance(d, GoTo) and d.node == "analyst"


def test_rerun_on_signal_continue_when_no_signal():
    assert isinstance(rerun_on_signal(_ctx("verify"), target="analyst"), Continue)


def test_rerun_on_named_routes_to_named_node():
    # A coherence check names WHICH node to re-run; the gate GoTo's exactly that.
    d = rerun_on_named(_ctx("consistency", ["extractor"]))
    assert isinstance(d, GoTo) and d.node == "extractor"


def test_rerun_on_named_first_of_multiple():
    d = rerun_on_named(_ctx("consistency", ["summary", "analyst"]))
    assert isinstance(d, GoTo) and d.node == "summary"


def test_rerun_on_named_continue_when_no_signal():
    assert isinstance(rerun_on_named(_ctx("consistency")), Continue)


# --- rerun_targets: pure envelope extraction, no I/O ---------------------------


def test_rerun_targets_none_or_non_dict():
    assert rerun_targets(None) == []
    assert rerun_targets("not a dict") == []


def test_rerun_targets_no_field():
    assert rerun_targets({"status": "verified", "agent": "domain-verifier"}) == []


def test_rerun_targets_list():
    assert rerun_targets({"status": "verified", "rerun_required": ["domain-analyst"]}) == ["domain-analyst"]


def test_rerun_targets_string_coerced_to_list():
    assert rerun_targets({"rerun_required": "coupling-analyst"}) == ["coupling-analyst"]


def test_rerun_targets_empty_string_ignored():
    assert rerun_targets({"rerun_required": "  "}) == []


# --- produced: genuine filesystem check of the agent's work product -----------


def test_produced_true_for_nonempty(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# hi")
    assert produced(p) is True


def test_produced_false_for_missing(tmp_path):
    assert produced(tmp_path / "nope.md") is False


def test_produced_false_for_empty(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("")
    assert produced(p) is False
