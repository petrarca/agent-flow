"""Stage E — the programmatic `run_config=` and the removal of the non-portable
FlowDef fields + the run_cli(default_*) params.

`run_config=` (a dict or a RunConfig) is the single run-side config channel for
run_flow / arun_flow / run_cli: agent_dir, backend, llm_concurrency, durations,
nodes, options, run_dir. It is the LOWEST explicit source (env/.env/--config/CLI
all override it). These tests pin delivery, precedence, and the removals.
"""

import anyio
import pytest

from agent_flow.flowdef import FlowDef, NodeDef
from agent_flow.registry import FlowRegistry


def _spy_flow():
    """A one-node flow whose mock agent records the invocation it received."""
    seen: dict = {}
    registry = FlowRegistry()

    @registry.mock_agent("a")
    def _cap(inv, ctx):  # noqa: ARG001
        seen["model"] = inv.model
        seen["agent_dir"] = inv.agent_dir
        seen["idle"] = inv.idle_timeout_s
        return {"status": "ok"}

    flow = FlowDef(name="t", nodes=[NodeDef(name="n", agent="a", duration="long")])
    return flow, registry, seen


# --- run_config= delivers the run-side settings -----------------------------


def test_run_flow_run_config_sets_durations(tmp_path):
    from agent_flow import run_flow

    flow, registry, seen = _spy_flow()
    run_flow(flow, registry=registry, run_dir=str(tmp_path), mock_agents=True, run_config={"durations": {"long": 900}})
    assert seen["idle"] == 900


def test_run_flow_run_config_sets_per_node_model(tmp_path):
    from agent_flow import run_flow

    flow, registry, seen = _spy_flow()
    run_flow(flow, registry=registry, run_dir=str(tmp_path), mock_agents=True, run_config={"nodes": {"n": {"model": "prov/big"}}})
    assert seen["model"] == "prov/big"


def test_run_flow_accepts_a_runconfig_instance(tmp_path):
    from agent_flow import RunConfig, run_flow

    flow, registry, seen = _spy_flow()
    rc = RunConfig(model="prov/rc")
    run_flow(flow, registry=registry, run_dir=str(tmp_path), mock_agents=True, run_config=rc)
    assert seen["model"] == "prov/rc"


def test_arun_flow_run_config(tmp_path):
    from agent_flow import arun_flow

    flow, registry, seen = _spy_flow()
    anyio.run(lambda: arun_flow(flow, registry=registry, run_dir=str(tmp_path), mock_agents=True, run_config={"model": "prov/a"}))
    assert seen["model"] == "prov/a"


def test_run_config_run_dir_used_when_no_explicit_arg(tmp_path):
    from agent_flow import run_flow

    flow, registry, seen = _spy_flow()
    out = tmp_path / "from-config"
    run_flow(flow, registry=registry, mock_agents=True, run_config={"run_dir": str(out), "durations": {"long": 5}})
    assert out.exists()  # the run created it


def test_explicit_run_dir_beats_run_config(tmp_path):
    from agent_flow import run_flow

    flow, registry, seen = _spy_flow()
    explicit = tmp_path / "explicit"
    rc = {"run_dir": str(tmp_path / "ignored"), "durations": {"long": 5}}
    run_flow(flow, registry=registry, run_dir=str(explicit), mock_agents=True, run_config=rc)
    assert explicit.exists()
    assert not (tmp_path / "ignored").exists()


# --- precedence: env > --config > run_config= base --------------------------


def test_cli_env_beats_run_config_base(monkeypatch):
    """run_config= is the LOWEST explicit source; an env var must override it."""
    from agent_flow.run_config import build_run_config

    monkeypatch.setenv("AGENT_FLOW_MODEL", "from-env")
    cfg = build_run_config(base={"model": "from-run-config"})
    assert cfg.model == "from-env"


def test_config_file_beats_run_config_base(tmp_path):
    from agent_flow.run_config import build_run_config

    p = tmp_path / "run.yml"
    p.write_text("model: from-config-file\n")
    cfg = build_run_config(config_file=str(p), base={"model": "from-run-config"})
    assert cfg.model == "from-config-file"


def test_run_config_base_used_when_nothing_higher():
    from agent_flow.run_config import build_run_config

    cfg = build_run_config(base={"model": "from-run-config"})
    assert cfg.model == "from-run-config"


def test_run_config_base_rejects_unknown_key():
    from agent_flow.run_config import build_run_config

    with pytest.raises(ValueError, match="unknown run config keys"):
        build_run_config(base={"bogus": 1})


# --- the removals ------------------------------------------------------------


def test_flowdef_rejects_agent_dir_backend_concurrency():
    from pydantic import ValidationError

    for kwargs in ({"agent_dir": "/x"}, {"backend": "prefect"}, {"llm_concurrency": 4}):
        with pytest.raises(ValidationError):
            FlowDef(name="t", nodes=[NodeDef(name="n", agent="a")], **kwargs)


def test_run_cli_no_longer_has_default_params():
    import inspect

    from agent_flow.cli import run_cli

    params = inspect.signature(run_cli).parameters
    assert "default_agent_dir" not in params
    assert "default_run_dir" not in params
    assert "run_config" in params


def test_run_flow_no_longer_has_the_folded_kwargs():
    import inspect

    from agent_flow import run_flow

    params = inspect.signature(run_flow).parameters
    for gone in ("durations", "node_overrides", "options"):
        assert gone not in params, f"{gone} should have folded into run_config="
    assert "run_config" in params


# --- run_config= via run_cli (CLI path) -------------------------------------


def test_run_cli_run_config_reaches_the_run(monkeypatch, tmp_path):
    """run_cli(run_config=...) becomes the base layer feeding the CLI's config."""
    from agent_flow.cli.commands.run import _build_and_run
    from agent_flow.cli.console import get_console
    from agent_flow.flowdef import compile_flow
    from agent_flow.run_config import build_run_config

    flow, registry, seen = _spy_flow()
    # Simulate what run_cli does: normalize run_config into the context, then the
    # run command builds the RunConfig with it as `base`.
    cfg = build_run_config(base={"agent_dir": "/from/run_config", "durations": {"long": 7}}, run_dir=str(tmp_path), mock_agents=True)
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
    )
    assert seen["agent_dir"] == "/from/run_config"
    assert seen["idle"] == 7
