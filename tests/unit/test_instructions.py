"""Unit tests for the instruction input plane: run-wide brief + per-node instructions.

Composition contract (final prompt order):
    [completion protocol] [run-wide shared_instructions] [per-node instructions] [work order]
"""

import tempfile
from pathlib import Path

import anyio

from agent_flow.engine import interpret
from agent_flow.gates import Continue
from agent_flow.node_builder import agent_node


def _capture_prompt(monkeypatch, node, *, shared="", params=None, run_dir=None, shared_context=(), node_instructions=None):
    """Run a agent-node with a stubbed EXECUTOR that captures the invocation.

    the agent-node builds a neutral AgentInvocation and hands it to an AgentExecutor;
    we stub get_executor to capture that invocation (its per-node prompt and the
    run-wide shared_* blocks stay SEPARATE fields — the composition contract).
    """
    from agent_flow.core.agent_runtime import AgentResult

    captured = {}

    class _FakeExecutor:
        name = "fake"

        async def run(self, inv):
            captured["prompt"] = inv.prompt
            captured["shared"] = inv.shared_instructions
            captured["shared_context"] = inv.shared_context
            return AgentResult(agent=inv.agent, exit_code=0, duration_s=0.0, control={"status": "ok"}, completion="completed")

    monkeypatch.setattr("agent_flow.node_builder.get_executor", lambda _runtime: _FakeExecutor())
    anyio.run(
        lambda: interpret(
            node,
            run_dir=Path(run_dir) if run_dir else Path(tempfile.gettempdir()),
            params=params or {},
            on_error=lambda n, e: "degraded",
            shared_instructions=shared,
            shared_context=tuple(shared_context),
            node_instructions=node_instructions or {},
        )
    )
    return captured


def test_shared_instructions_forwarded_to_run_agent(monkeypatch, tmp_path):
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(monkeypatch, node, shared="Use code-graph alongside RAG.")
    # The agent-node forwards the run-wide brief to run_agent verbatim.
    assert cap["shared"] == "Use code-graph alongside RAG."


def test_per_node_instructions_prepended_to_work_order(monkeypatch):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="Prefer a compact table.")
    cap = _capture_prompt(monkeypatch, node)
    p = cap["prompt"]
    assert "Prefer a compact table." in p
    assert "<K>v</K>" in p
    assert p.find("Prefer a compact table.") < p.find("<K>v</K>")  # instructions before work order


def test_instructions_are_templated(monkeypatch):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="Focus on {product_key}.")
    cap = _capture_prompt(monkeypatch, node, params={"product_key": "acme"})
    assert "Focus on acme." in cap["prompt"]


def test_runtime_node_instruction_appended_after_build_time(monkeypatch):
    # The run-time per-node instruction (CLI --instruct / config node_instructions)
    # is appended AFTER the build-time instruction and BEFORE the work order, so
    # it is the last standing guidance (additive, last-word override).
    node = agent_node("analyst", "agent-x", inputs={"K": "v"}, instructions="Prefer a compact table.")
    cap = _capture_prompt(monkeypatch, node, node_instructions={"analyst": "Ignore that; produce the full breakdown."})
    p = cap["prompt"]
    assert p.find("Prefer a compact table.") < p.find("produce the full breakdown") < p.find("<K>v</K>")


def test_runtime_node_instruction_only_targets_named_node(monkeypatch):
    node = agent_node("summary", "agent-x", inputs={"K": "v"})
    # an instruction for a DIFFERENT node must not appear
    cap = _capture_prompt(monkeypatch, node, node_instructions={"analyst": "analyst-only note"})
    assert "analyst-only note" not in cap["prompt"]


def test_runtime_node_instruction_is_templated(monkeypatch):
    node = agent_node("analyst", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(monkeypatch, node, params={"product_key": "acme"}, node_instructions={"analyst": "Focus on {product_key}."})
    assert "Focus on acme." in cap["prompt"]


def test_no_instructions_leaves_plain_work_order(monkeypatch):
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(monkeypatch, node)
    assert cap["prompt"].strip() == "<K>v</K>"
    assert cap["shared"] == ""


def test_per_node_context_content_injected_before_instructions(monkeypatch, tmp_path):
    (tmp_path / "rules.md").write_text("RULE: always X.")
    node = agent_node("n", "agent-x", inputs={"K": "v"}, context=("rules.md",), instructions="do the thing")
    cap = _capture_prompt(monkeypatch, node, run_dir=tmp_path)
    p = cap["prompt"]
    assert "RULE: always X." in p
    # order: context content -> instructions -> work order
    assert p.find("RULE: always X.") < p.find("do the thing") < p.find("<K>v</K>")


def test_shared_context_sources_read_and_forwarded(monkeypatch, tmp_path):
    (tmp_path / "sec.md").write_text("SECURITY: never log secrets.")
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(monkeypatch, node, run_dir=tmp_path, shared_context=("sec.md",))
    # The agent-node reads the run-wide sources into CONTENT and forwards it.
    assert "SECURITY: never log secrets." in cap["shared_context"]


def test_missing_context_source_does_not_crash(monkeypatch, tmp_path):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, context=("nope.md",))
    cap = _capture_prompt(monkeypatch, node, run_dir=tmp_path)
    assert "<K>v</K>" in cap["prompt"]  # ran fine; missing context skipped


def test_gate_still_runs_with_instructions(monkeypatch):
    # Sanity: adding instructions doesn't disturb gate wiring.
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="x", gate=lambda ctx: Continue())
    cap = _capture_prompt(monkeypatch, node)
    assert "<K>v</K>" in cap["prompt"]
