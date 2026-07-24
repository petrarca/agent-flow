"""Unit tests for the instruction input plane: run-wide brief + per-node instructions.

Composition contract (final prompt order):
    [completion protocol] [run-wide shared_instructions] [per-node instructions] [work order]
"""

import tempfile
from pathlib import Path

from agent_flow.batteries import agent_node
from agent_flow.engine import interpret
from agent_flow.gates import Continue


def _capture_prompt(monkeypatch, node, *, shared="", params=None, run_dir=None, shared_context=()):
    """Run a batteries node with a stubbed run_agent that captures the prompt."""
    captured = {}

    class _Result:
        control = {"status": "ok"}
        tokens = cost = events = 0
        completion = "sidecar"
        duration_s = 0.0
        result_valid = True
        result_obj = None
        result_errors = ()

    def fake_run_agent(*, agent, prompt, shared_instructions="", shared_context="", **kw):
        captured["prompt"] = prompt
        captured["shared"] = shared_instructions
        captured["shared_context"] = shared_context
        return _Result()

    monkeypatch.setattr("agent_flow.batteries.run_agent", fake_run_agent)
    interpret(
        node,
        run_dir=Path(run_dir) if run_dir else Path(tempfile.gettempdir()),
        params=params or {},
        on_error=lambda n, e: "degraded",
        shared_instructions=shared,
        shared_context=tuple(shared_context),
    )
    return captured


def test_shared_instructions_forwarded_to_run_agent(monkeypatch, tmp_path):
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(monkeypatch, node, shared="Use code-graph alongside RAG.")
    # The batteries node forwards the run-wide brief to run_agent verbatim.
    assert cap["shared"] == "Use code-graph alongside RAG."


def test_per_node_instructions_prepended_to_work_order(monkeypatch):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="Prefer a compact table.")
    cap = _capture_prompt(monkeypatch, node)
    p = cap["prompt"]
    assert "Prefer a compact table." in p
    assert "K: v" in p
    assert p.find("Prefer a compact table.") < p.find("K: v")  # instructions before work order


def test_instructions_are_templated(monkeypatch):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="Focus on {product_key}.")
    cap = _capture_prompt(monkeypatch, node, params={"product_key": "acme"})
    assert "Focus on acme." in cap["prompt"]


def test_no_instructions_leaves_plain_work_order(monkeypatch):
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(monkeypatch, node)
    assert cap["prompt"].strip() == "K: v"
    assert cap["shared"] == ""


def test_per_node_context_content_injected_before_instructions(monkeypatch, tmp_path):
    (tmp_path / "rules.md").write_text("RULE: always X.")
    node = agent_node("n", "agent-x", inputs={"K": "v"}, context=("rules.md",), instructions="do the thing")
    cap = _capture_prompt(monkeypatch, node, run_dir=tmp_path)
    p = cap["prompt"]
    assert "RULE: always X." in p
    # order: context content -> instructions -> work order
    assert p.find("RULE: always X.") < p.find("do the thing") < p.find("K: v")


def test_shared_context_sources_read_and_forwarded(monkeypatch, tmp_path):
    (tmp_path / "sec.md").write_text("SECURITY: never log secrets.")
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(monkeypatch, node, run_dir=tmp_path, shared_context=("sec.md",))
    # The batteries node reads the run-wide sources into CONTENT and forwards it.
    assert "SECURITY: never log secrets." in cap["shared_context"]


def test_missing_context_source_does_not_crash(monkeypatch, tmp_path):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, context=("nope.md",))
    cap = _capture_prompt(monkeypatch, node, run_dir=tmp_path)
    assert "K: v" in cap["prompt"]  # ran fine; missing context skipped


def test_gate_still_runs_with_instructions(monkeypatch):
    # Sanity: adding instructions doesn't disturb gate wiring.
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="x", gate=lambda ctx: Continue())
    cap = _capture_prompt(monkeypatch, node)
    assert "K: v" in cap["prompt"]
