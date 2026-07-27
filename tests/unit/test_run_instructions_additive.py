"""Stage G — run-wide instructions are ADDITIVE, and the 0.3.0 silent drop is fixed.

The flow's STANDING `run_instructions` always renders; this run's ADDITION (the
-i / --instructions value, or run_config `instructions`) renders AFTER it as its
own block. Before this: run_cli dropped the flow's run_instructions entirely, and
(mirror) run_flow ignored the run-config instructions. Both are fixed; a flow that
declares NO run_instructions behaves exactly as 0.3.0 did.

Rendered prompt order (the relevant slice):
    ## Run-wide instructions            <- flow's standing brief
    ## Additional instructions for this run   <- this run's -i addition
"""

import tempfile
from pathlib import Path

import anyio
import pytest

from agent_flow.engine import interpret
from agent_flow.node_builder import agent_node

STANDING = "STANDING-BRIEF-MARKER"
ADDITION = "ADDITION-MARKER"


def _prompt(spy, *, run_instructions="", run_additional_instructions=""):
    """Render one node's prompt through the shared executor spy; return the text."""
    anyio.run(
        lambda: interpret(
            agent_node("n", "a", inputs={"X": "1"}),
            run_dir=Path(tempfile.gettempdir()),
            params={},
            on_error=lambda n, e: "degraded",
            run_instructions=run_instructions,
            run_additional_instructions=run_additional_instructions,
        )
    )
    return spy.inv.prompt


# --- both blocks present, in order ------------------------------------------


def test_standing_brief_renders(spy_executor):
    assert STANDING in _prompt(spy_executor, run_instructions=STANDING)


def test_addition_renders(spy_executor):
    assert ADDITION in _prompt(spy_executor, run_additional_instructions=ADDITION)


def test_both_render_standing_before_addition(spy_executor):
    p = _prompt(spy_executor, run_instructions=STANDING, run_additional_instructions=ADDITION)
    assert STANDING in p and ADDITION in p
    assert p.index(STANDING) < p.index(ADDITION)  # standing first, addition appended


def test_addition_has_its_own_scoped_heading(spy_executor):
    p = _prompt(spy_executor, run_instructions=STANDING, run_additional_instructions=ADDITION)
    assert "## Run-wide instructions" in p
    # Scoped title (not the per-node "for this step" one) so the two never collide.
    assert "## Additional run-wide instructions" in p


def test_no_instructions_leaves_no_run_wide_headings(spy_executor):
    """The 0.3.0-identical case: nothing declared, nothing added -> no heading."""
    p = _prompt(spy_executor)
    assert "Run-wide instructions" not in p


def test_only_standing_when_no_addition(spy_executor):
    p = _prompt(spy_executor, run_instructions=STANDING)
    assert "## Run-wide instructions" in p
    assert "Additional run-wide instructions" not in p


# --- the fix at BOTH entry points -------------------------------------------
#
# The bug was asymmetric: run_cli dropped the flow's run_instructions; run_flow
# dropped the run-config instructions. Assert BOTH deliver BOTH.


@pytest.fixture
def spy():
    from agent_flow.registry import FlowRegistry

    seen: dict[str, str] = {}
    registry = FlowRegistry()

    @registry.mock_agent("a")
    def _cap(inv, ctx):  # noqa: ARG001
        seen["prompt"] = inv.prompt
        return {"status": "ok"}

    return registry, seen


def test_programmatic_run_flow_keeps_flow_brief_and_adds(spy, tmp_path):
    from agent_flow import run_flow
    from agent_flow.flowdef import FlowDef, NodeDef

    registry, seen = spy
    flow = FlowDef(name="t", run_instructions=STANDING, nodes=[NodeDef(name="n", agent="a", inputs={"X": "1"})])
    # run-config `instructions` is the addition on the programmatic path.
    run_flow(flow, registry=registry, run_dir=str(tmp_path), mock_agents=True, run_config={"instructions": ADDITION})
    assert STANDING in seen["prompt"], "run_flow must NOT drop the flow's run_instructions"
    assert ADDITION in seen["prompt"], "run_flow must apply the run-config instructions"


def test_cli_path_keeps_flow_brief_and_adds(spy, tmp_path):
    from agent_flow.cli.commands.run import _build_and_run
    from agent_flow.cli.console import get_console
    from agent_flow.flowdef import FlowDef, NodeDef, compile_flow
    from agent_flow.run_config import build_run_config

    registry, seen = spy
    flow = FlowDef(name="t", run_instructions=STANDING, nodes=[NodeDef(name="n", agent="a", inputs={"X": "1"})])
    cfg = build_run_config(run_dir=str(tmp_path), mock_agents=True, instructions=ADDITION)
    _build_and_run(
        compile_flow(flow, registry),
        {},
        cfg,
        get_console(),
        name="t",
        llm_tag="llm",
        on_event_factory=None,
        on_node_event=None,
        render_results=False,
        registry=registry,
        run_instructions=flow.run_instructions,  # what run_cli threads from the FlowDef
    )
    assert STANDING in seen["prompt"], "run_cli must NOT drop the FlowDef's run_instructions (the 0.3.0 bug)"
    assert ADDITION in seen["prompt"], "run_cli must apply -i/--instructions"
