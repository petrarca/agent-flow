"""Unit tests for report_signals — the file-based signals the orchestrator reads."""

import json

from agent_flow.report_signals import produced, rerun_from_sidecar


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
