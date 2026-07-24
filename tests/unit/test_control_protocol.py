"""Unit tests for the injected control-file protocol preamble."""

from agent_flow.control_protocol import build_control_preamble


def test_preamble_contains_control_file_kv_line():
    # The mock agent (and LLM agents) locate the sidecar via a KEY: value line.
    p = build_control_preamble("analyst", "/abs/analyst.control.json")
    assert "CONTROL_FILE: /abs/analyst.control.json" in p


def test_preamble_echoes_agent_name():
    p = build_control_preamble("tech-stack-verifier", "/x/c.json")
    assert '"agent": "tech-stack-verifier"' in p


def test_preamble_documents_envelope_and_result():
    p = build_control_preamble("a", "/x/c.json")
    assert '"status"' in p
    assert '"result"' in p
    # No artifact field in the contract.
    assert "artifact" not in p
