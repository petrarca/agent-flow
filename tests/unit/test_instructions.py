"""Unit tests for the instruction input plane: run-wide brief + per-node instructions.

Composition contract (final prompt order):
    [completion protocol] [run_context] [run_instructions] [run additional instructions]
    [node context] [node instructions] [node runtime instructions] [attempt] [work order]
"""

import tempfile
from pathlib import Path

import anyio

from agent_flow.engine import interpret
from agent_flow.gates import Continue
from agent_flow.node_builder import agent_node


def _capture_prompt(spy, node, *, shared="", params=None, run_dir=None, run_context=(), node_instructions=None):
    """Run a agent-node through the shared executor spy and return its invocation.

    The agent-node builds a neutral AgentInvocation and hands it to an
    AgentExecutor; the spy captures it (the per-node prompt and the run-wide
    blocks stay SEPARATE fields — the composition contract).
    """
    anyio.run(
        lambda: interpret(
            node,
            run_dir=Path(run_dir) if run_dir else Path(tempfile.gettempdir()),
            params=params or {},
            on_error=lambda n, e: "degraded",
            run_instructions=shared,
            run_context=tuple(run_context),
            # The {node: text} the tests pass is projected into the richer
            # per-node override shape the engine now consumes.
            node_overrides={n: {"instructions": t} for n, t in (node_instructions or {}).items()},
        )
    )
    return spy.inv


def test_run_instructions_forwarded_to_run_agent(spy_executor, tmp_path):
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(spy_executor, node, shared="Use code-graph alongside RAG.")
    # The agent-node forwards the run-wide brief to run_agent verbatim.
    assert cap.run_instructions == "Use code-graph alongside RAG."


def test_per_node_instructions_prepended_to_work_order(spy_executor):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="Prefer a compact table.")
    cap = _capture_prompt(spy_executor, node)
    p = cap.prompt
    assert "Prefer a compact table." in p
    assert "<K>v</K>" in p
    assert p.find("Prefer a compact table.") < p.find("<K>v</K>")  # instructions before work order


def test_instructions_are_templated(spy_executor):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="Focus on {product_key}.")
    cap = _capture_prompt(spy_executor, node, params={"product_key": "acme"})
    assert "Focus on acme." in cap.prompt


def test_runtime_node_instruction_appended_after_build_time(spy_executor):
    # The run-time per-node instruction (CLI --instruct / config node_instructions)
    # is appended AFTER the build-time instruction and BEFORE the work order, so
    # it is the last standing guidance (additive, last-word override).
    node = agent_node("analyst", "agent-x", inputs={"K": "v"}, instructions="Prefer a compact table.")
    cap = _capture_prompt(spy_executor, node, node_instructions={"analyst": "Ignore that; produce the full breakdown."})
    p = cap.prompt
    assert p.find("Prefer a compact table.") < p.find("produce the full breakdown") < p.find("<K>v</K>")


def test_runtime_node_instruction_only_targets_named_node(spy_executor):
    node = agent_node("summary", "agent-x", inputs={"K": "v"})
    # an instruction for a DIFFERENT node must not appear
    cap = _capture_prompt(spy_executor, node, node_instructions={"analyst": "analyst-only note"})
    assert "analyst-only note" not in cap.prompt


def test_runtime_node_instruction_is_templated(spy_executor):
    node = agent_node("analyst", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(spy_executor, node, params={"product_key": "acme"}, node_instructions={"analyst": "Focus on {product_key}."})
    assert "Focus on acme." in cap.prompt


def test_no_instructions_leaves_plain_work_order(spy_executor):
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(spy_executor, node)
    assert cap.prompt.strip() == "<K>v</K>"
    assert cap.run_instructions == ""


def test_per_node_context_content_injected_before_instructions(spy_executor, tmp_path):
    (tmp_path / "rules.md").write_text("RULE: always X.")
    node = agent_node("n", "agent-x", inputs={"K": "v"}, context=("rules.md",), instructions="do the thing")
    cap = _capture_prompt(spy_executor, node, run_dir=tmp_path)
    p = cap.prompt
    assert "RULE: always X." in p
    # order: context content -> instructions -> work order
    assert p.find("RULE: always X.") < p.find("do the thing") < p.find("<K>v</K>")


def test_run_context_sources_read_and_forwarded(spy_executor, tmp_path):
    (tmp_path / "sec.md").write_text("SECURITY: never log secrets.")
    node = agent_node("n", "agent-x", inputs={"K": "v"})
    cap = _capture_prompt(spy_executor, node, run_dir=tmp_path, run_context=("sec.md",))
    # The agent-node reads the run-wide sources into CONTENT and forwards it.
    assert "SECURITY: never log secrets." in cap.run_context


def test_missing_context_source_does_not_crash(spy_executor, tmp_path):
    node = agent_node("n", "agent-x", inputs={"K": "v"}, context=("nope.md",))
    cap = _capture_prompt(spy_executor, node, run_dir=tmp_path)
    assert "<K>v</K>" in cap.prompt  # ran fine; missing context skipped


def test_gate_still_runs_with_instructions(spy_executor):
    # Sanity: adding instructions doesn't disturb gate wiring.
    node = agent_node("n", "agent-x", inputs={"K": "v"}, instructions="x", gate=lambda ctx: Continue())
    cap = _capture_prompt(spy_executor, node)
    assert "<K>v</K>" in cap.prompt
