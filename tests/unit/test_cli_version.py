"""Unit tests for the `version` CLI command (two-layer version output).

The command prints the consumer's app version (primary, from
`run_cli(version=...)`) plus the agent-flow framework version (secondary). These
tests drive the command module's `register()` against a minimal Typer app so no
real pipeline run is needed.
"""

import typer
from typer.testing import CliRunner

import agent_flow
from agent_flow.cli.commands import version as version_cmd
from agent_flow.cli.context import RunCliContext

_runner = CliRunner()


def _app(ctx: RunCliContext) -> typer.Typer:
    app = typer.Typer()

    # A no-op callback keeps the multi-command group structure so `version` stays
    # an explicit subcommand (Typer collapses a lone command to a direct one),
    # mirroring how run_cli assembles the real app.
    @app.callback()
    def _main() -> None:
        pass

    version_cmd.register(app, ctx)
    return app


def _ctx(**kw) -> RunCliContext:
    base = dict(build_nodes=lambda: [], name="my-pipeline", llm_tag="llm", default_agent_dir="", default_run_dir="", params_model=None)
    base.update(kw)
    return RunCliContext(**base)


def test_version_shows_both_when_consumer_version_given():
    res = _runner.invoke(_app(_ctx(version="0.3.1")), ["version"])
    assert res.exit_code == 0
    assert f"my-pipeline 0.3.1 (agent-flow {agent_flow.__version__})" in res.stdout


def test_version_shows_agent_flow_only_when_no_consumer_version():
    res = _runner.invoke(_app(_ctx()), ["version"])
    assert res.exit_code == 0
    # No dangling app-version slot; just "<name> (agent-flow <ver>)".
    assert f"my-pipeline (agent-flow {agent_flow.__version__})" in res.stdout


def test_agent_flow_version_is_a_nonempty_string():
    assert isinstance(agent_flow.__version__, str) and agent_flow.__version__
