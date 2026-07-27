"""The run-wide context channel must reach agents from BOTH entry points.

Regression: `flowdef.compile` passed `run_context` to `build_flow`, but the CLI
path did not — so a FlowDef declaring run-wide rules worked under `run_flow` and
silently dropped them under `run_cli`. Silent, because the agent simply never saw
the rules; nothing failed. Both paths are asserted here so they cannot drift.
"""

import tempfile
from pathlib import Path

import anyio
import pytest

from agent_flow.flowdef import FlowDef, NodeDef
from agent_flow.registry import FlowRegistry

MARKER = "SECRET-RULE-MARKER"


@pytest.fixture
def flow_with_run_context(tmp_path):
    rules = tmp_path / "rules.md"
    rules.write_text(f"{MARKER}: never log secrets.")
    seen: dict[str, str] = {}

    registry = FlowRegistry()

    @registry.mock_agent("a")
    def _capture(inv, ctx):  # noqa: ARG001
        seen["run_context"] = inv.run_context
        return {"status": "ok"}

    flow = FlowDef(name="t", run_context=[str(rules)], nodes=[NodeDef(name="n", agent="a")])
    return flow, registry, seen


def test_programmatic_run_flow_delivers_run_context(flow_with_run_context):
    from agent_flow import run_flow

    flow, registry, seen = flow_with_run_context
    with tempfile.TemporaryDirectory() as d:
        run_flow(flow, registry=registry, run_dir=d, mock_agents=True)
    assert MARKER in seen["run_context"]


def test_cli_path_delivers_run_context(flow_with_run_context):
    """The same FlowDef, run the way `run_cli` runs it."""
    from agent_flow.cli.commands.run import _build_and_run
    from agent_flow.cli.console import get_console
    from agent_flow.flowdef import compile_flow
    from agent_flow.run_config import build_run_config

    flow, registry, seen = flow_with_run_context
    with tempfile.TemporaryDirectory() as d:
        cfg = build_run_config(run_dir=d, mock_agents=True)
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
            run_context=tuple(flow.run_context),  # what run_cli threads from the FlowDef
        )
    assert MARKER in seen["run_context"], "run_cli must not drop the FlowDef's run-wide context"


def test_run_cli_puts_the_flowdefs_run_context_on_the_cli_context(flow_with_run_context, monkeypatch):
    """The exact wire that was missing: run_cli -> RunCliContext.run_context.

    Spies on the command registration (where the built context is handed over) so
    a regression in `cli/app.py` is caught here, not only end-to-end.
    """
    import agent_flow.cli.app as app_mod

    flow, registry, _ = flow_with_run_context
    captured = {}

    def _spy(app, ctx):
        captured["ctx"] = ctx
        raise SystemExit(0)  # stop before the Typer app takes over argv

    monkeypatch.setattr("agent_flow.cli.commands.run.register", _spy)
    with pytest.raises(SystemExit):
        app_mod.run_cli(flow, registry=registry)

    assert tuple(captured["ctx"].run_context) == tuple(flow.run_context)


def test_run_cli_with_a_plain_node_builder_has_no_run_context(monkeypatch):
    """The non-FlowDef form has no flow-level declaration — must not blow up."""
    import agent_flow.cli.app as app_mod

    captured = {}

    def _spy(app, ctx):
        captured["ctx"] = ctx
        raise SystemExit(0)

    monkeypatch.setattr("agent_flow.cli.commands.run.register", _spy)
    with pytest.raises(SystemExit):
        app_mod.run_cli(lambda: [])
    assert captured["ctx"].run_context == ()


def test_node_level_context_is_unaffected(tmp_path):
    """The node-scoped channel is separate and was never affected."""
    from agent_flow import agent_node, build_flow

    node_rules = tmp_path / "node.md"
    node_rules.write_text("NODE-ONLY-MARKER")
    seen = {}

    async def impl(inv):
        seen["node_ctx"] = inv.prompt
        seen["run_ctx"] = inv.run_context
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl, context=[str(node_rules)])
    with tempfile.TemporaryDirectory() as d:
        anyio.run(lambda: build_flow([n], name="t")(run_dir=d))
    assert "NODE-ONLY-MARKER" in seen["node_ctx"]
    assert seen["run_ctx"] == ""  # no run-wide context declared


def test_missing_run_context_source_warns_and_does_not_crash(tmp_path):
    from agent_flow import agent_node, build_flow

    seen = {}

    async def impl(inv):
        seen["run_ctx"] = inv.run_context
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl)
    with tempfile.TemporaryDirectory() as d:
        anyio.run(lambda: build_flow([n], name="t", run_context=[str(Path(d) / "nope-*.md")])(run_dir=d))
    assert seen["run_ctx"] == ""
