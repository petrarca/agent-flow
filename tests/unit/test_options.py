"""The runtime-specific `options` bag — run-wide + per-node, merged at the seam.

`options` is an OPEN dict the runtime interprets (e.g. serve_url for a remote
runtime); the engine never looks inside. These tests pin that it reaches the
executor seam, that per-node merges OVER run-wide (key by key), and that both
entry points deliver it.
"""

import tempfile
from pathlib import Path

import anyio
import pytest

from agent_flow.engine import interpret
from agent_flow.node_builder import agent_node


def _capture_options(spy, node, *, options=None, node_overrides=None):
    """Run one node through the shared spy; return the options get_executor got."""
    anyio.run(
        lambda: interpret(
            node,
            run_dir=Path(tempfile.gettempdir()),
            params={},
            on_error=lambda n, e: "degraded",
            options=options or {},
            node_overrides=node_overrides or {},
        )
    )
    return spy.kwargs.get("options")


def test_run_wide_options_reach_the_executor(spy_executor):
    node = agent_node("n", "a")
    assert _capture_options(spy_executor, node, options={"serve_url": "http://x:4096"}) == {"serve_url": "http://x:4096"}


def test_no_options_yields_empty(spy_executor):
    assert _capture_options(spy_executor, agent_node("n", "a")) == {}


def test_per_node_options_merge_over_run_wide(spy_executor):
    node = agent_node("n", "a")
    opts = _capture_options(
        spy_executor,
        node,
        options={"serve_url": "http://run-wide:4096", "keep": "yes"},
        node_overrides={"n": {"options": {"serve_url": "http://node:5000"}}},
    )
    assert opts == {"serve_url": "http://node:5000", "keep": "yes"}  # overridden + inherited


def test_options_is_an_open_bag(spy_executor):
    node = agent_node("n", "a")
    assert _capture_options(spy_executor, node, options={"anything": 1, "nested": {"a": 2}})["nested"] == {"a": 2}


# --- get_executor reads serve_url from the bag ------------------------------


def test_options_do_not_break_a_subprocess_runtime():
    from agent_flow.runners import get_executor

    # opencode needs no endpoint; passing options (incl. an unknown key) is inert.
    assert get_executor("opencode", options={"serve_url": "http://x", "unknown": 1}) is not None


@pytest.fixture
def endpoint_runtime(monkeypatch):
    """Register a throwaway runtime whose spec REQUIRES an endpoint, so the
    serve_url resolution in get_executor becomes observable (a subprocess runtime
    ignores serve_url, which is why the opencode-based test could not see it)."""
    import agent_flow.runners as R
    from agent_flow.runners.spec import TRANSPORT_HTTP_SSE, RunnerSpec

    class _RemoteRunner:
        def spec(self):
            return RunnerSpec(runtime="fake", mode="remote", transport=TRANSPORT_HTTP_SSE, name="fake-remote", needs_endpoint=True)

        def name(self):
            return "fake-remote"

    saved = dict(R._REGISTRY)
    R._REGISTRY["fake-remote"] = _RemoteRunner()  # direct insert; bypass the duplicate guard
    yield
    R._REGISTRY.clear()
    R._REGISTRY.update(saved)


def test_endpoint_runtime_errors_without_a_serve_url(endpoint_runtime):
    """No serve_url anywhere -> the endpoint check fails with a clear message."""
    from agent_flow.runners import get_executor

    with pytest.raises(ValueError, match="requires a serve_url"):
        get_executor("fake-remote")


def test_bag_serve_url_satisfies_the_endpoint_check(endpoint_runtime):
    """When the bag supplies serve_url, get_executor must get PAST the endpoint
    check — proving it read the bag. (The http-sse ServeExecutor is not yet
    implemented, so it then fails on the lazy import: a DIFFERENT error, which is
    exactly what distinguishes "read the bag" from "ignored the bag".)"""
    from agent_flow.runners import get_executor

    with pytest.raises(ModuleNotFoundError):  # got past the endpoint check, into serve_executor
        get_executor("fake-remote", options={"serve_url": "http://bag:9"})


def test_explicit_serve_url_also_satisfies_the_check(endpoint_runtime):
    from agent_flow.runners import get_executor

    with pytest.raises(ModuleNotFoundError):
        get_executor("fake-remote", serve_url="http://explicit")


# --- NodeRunConfig / RunConfig carry it -------------------------------------


def test_noderunconfig_and_runconfig_have_options():
    from agent_flow import NodeRunConfig
    from agent_flow.run_config import build_run_config

    assert NodeRunConfig(options={"k": "v"}).options == {"k": "v"}
    assert build_run_config().options == {}


def test_yaml_options_sections(tmp_path):
    from agent_flow.run_config import build_run_config

    p = tmp_path / "run.yml"
    p.write_text('runtime: mock\noptions: {serve_url: "http://run:4096"}\nnodes:\n  n: {options: {serve_url: "http://node:5000"}}\n')
    cfg = build_run_config(config_file=str(p))
    assert cfg.options == {"serve_url": "http://run:4096"}
    assert cfg.nodes["n"].options == {"serve_url": "http://node:5000"}


# --- both entry points ------------------------------------------------------
#
# mock_agents routes to MockExecutor (never get_executor), so to observe the
# options at the seam we spy on get_executor and run without mock mode.


def test_programmatic_run_flow_forwards_options(spy_executor, tmp_path):
    from agent_flow import run_flow
    from agent_flow.flowdef import FlowDef, NodeDef
    from agent_flow.registry import FlowRegistry

    flow = FlowDef(name="t", nodes=[NodeDef(name="n", agent="a")])
    run_flow(flow, registry=FlowRegistry(), run_dir=str(tmp_path), run_config={"options": {"serve_url": "http://p:1"}})
    assert spy_executor.kwargs.get("options") == {"serve_url": "http://p:1"}


def test_cli_path_forwards_options(spy_executor, tmp_path):
    from agent_flow.cli.commands.run import _build_and_run
    from agent_flow.cli.console import get_console
    from agent_flow.flowdef import FlowDef, NodeDef, compile_flow
    from agent_flow.registry import FlowRegistry
    from agent_flow.run_config import build_run_config

    flow = FlowDef(name="t", nodes=[NodeDef(name="n", agent="a")])
    registry = FlowRegistry()
    cfg = build_run_config(run_dir=str(tmp_path))
    cfg.options = {"serve_url": "http://cli:2"}
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
    assert spy_executor.kwargs.get("options") == {"serve_url": "http://cli:2"}, "run_cli must forward run-wide options"
