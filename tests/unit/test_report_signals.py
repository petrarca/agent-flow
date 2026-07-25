"""Unit tests for report_signals — the file-based signals the orchestrator reads."""

import json
from pathlib import Path

from agent_flow.core.report_signals import produced, rerun_from_sidecar
from agent_flow.gates import Continue, GateContext, GoTo, rerun_on_named, rerun_on_signal


class _N:
    """Minimal node stand-in (gates read node.name)."""

    def __init__(self, name):
        self.name = name


def _ctx(run_dir, node_name):
    return GateContext(result={}, node=_N(node_name), run_dir=Path(run_dir), cycles=0, params={})


def _sidecar(run_dir, node_name, rerun):
    payload = {"status": "verified", "agent": "x"}
    if rerun is not None:
        payload["rerun_required"] = rerun
    (Path(run_dir) / f"{node_name}.control.json").write_text(json.dumps(payload))


def test_rerun_on_signal_fixed_target_when_signalled(tmp_path):
    _sidecar(tmp_path, "verify", ["analyst"])
    d = rerun_on_signal(target="analyst")(_ctx(tmp_path, "verify"))
    assert isinstance(d, GoTo) and d.node == "analyst"


def test_rerun_on_signal_continue_when_no_signal(tmp_path):
    _sidecar(tmp_path, "verify", None)
    assert isinstance(rerun_on_signal(target="analyst")(_ctx(tmp_path, "verify")), Continue)


def test_rerun_on_named_routes_to_named_node(tmp_path):
    # A coherence check names WHICH node to re-run; the gate GoTo's exactly that.
    _sidecar(tmp_path, "consistency", ["extractor"])
    d = rerun_on_named()(_ctx(tmp_path, "consistency"))
    assert isinstance(d, GoTo) and d.node == "extractor"


def test_rerun_on_named_first_of_multiple(tmp_path):
    _sidecar(tmp_path, "consistency", ["summary", "analyst"])
    d = rerun_on_named()(_ctx(tmp_path, "consistency"))
    assert isinstance(d, GoTo) and d.node == "summary"


def test_rerun_on_named_continue_when_no_signal(tmp_path):
    _sidecar(tmp_path, "consistency", None)
    assert isinstance(rerun_on_named()(_ctx(tmp_path, "consistency")), Continue)


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


def test_rerun_from_sidecar_missing_file(tmp_path):
    assert rerun_from_sidecar(tmp_path / "x.control.json") == []


def test_rerun_from_sidecar_no_field(tmp_path):
    p = tmp_path / "x.control.json"
    p.write_text(json.dumps({"status": "verified", "agent": "domain-verifier"}))
    assert rerun_from_sidecar(p) == []


def test_rerun_from_sidecar_list(tmp_path):
    p = tmp_path / "x.control.json"
    p.write_text(json.dumps({"status": "verified", "rerun_required": ["domain-analyst"]}))
    assert rerun_from_sidecar(p) == ["domain-analyst"]


def test_rerun_from_sidecar_string_coerced_to_list(tmp_path):
    p = tmp_path / "x.control.json"
    p.write_text(json.dumps({"rerun_required": "coupling-analyst"}))
    assert rerun_from_sidecar(p) == ["coupling-analyst"]


def test_rerun_from_sidecar_invalid_json(tmp_path):
    p = tmp_path / "x.control.json"
    p.write_text("{ not json")
    assert rerun_from_sidecar(p) == []
