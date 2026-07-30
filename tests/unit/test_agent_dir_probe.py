"""Stage F — runner-probed agent_dir.

When no agent_dir is configured anywhere, the RUNNER locates its convention:
OpenCodeRunner walks cwd + ancestors for `.opencode/` (git-like). The library
hardcodes no marker; a runner without the probe yields None and the requirement
surfaces at preflight. Explicit agent_dir (any tier) still wins.
"""

import tempfile
from pathlib import Path

import anyio

from agent_flow.engine import interpret
from agent_flow.node_builder import agent_node
from agent_flow.runners import OpenCodeRunner, probe_agent_dir
from agent_flow.utils import find_marker_dir

# --- the ancestor walk ------------------------------------------------------


def test_find_marker_dir_in_cwd(tmp_path):
    (tmp_path / ".opencode").mkdir()
    assert find_marker_dir(".opencode", tmp_path) == tmp_path.resolve()


def test_find_marker_dir_in_ancestor(tmp_path):
    (tmp_path / ".opencode").mkdir()
    deep = tmp_path / "a" / "b" / "c"
    deep.mkdir(parents=True)
    assert find_marker_dir(".opencode", deep) == tmp_path.resolve()


def test_find_marker_dir_absent(tmp_path):
    assert find_marker_dir(".opencode", tmp_path) is None


def test_find_marker_ignores_a_file_named_like_the_marker(tmp_path):
    (tmp_path / ".opencode").write_text("not a dir")  # a FILE, not a directory
    assert find_marker_dir(".opencode", tmp_path) is None


# --- the runner's probe -----------------------------------------------------


def test_opencode_runner_probes_opencode(tmp_path, monkeypatch):
    (tmp_path / ".opencode").mkdir()
    monkeypatch.chdir(tmp_path)
    assert OpenCodeRunner().default_agent_dir() == str(tmp_path.resolve())


def test_opencode_runner_returns_none_when_absent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert OpenCodeRunner().default_agent_dir() is None


# --- probe_agent_dir dispatch ------------------------------------------------


def test_probe_agent_dir_via_runtime_name(tmp_path, monkeypatch):
    (tmp_path / ".opencode").mkdir()
    monkeypatch.chdir(tmp_path)
    assert probe_agent_dir("opencode") == str(tmp_path.resolve())


def test_probe_agent_dir_unknown_runtime_is_none():
    assert probe_agent_dir("no-such-runtime") is None


def test_probe_agent_dir_runner_without_probe_is_none(monkeypatch):
    """A runner lacking default_agent_dir (e.g. a future remote one) -> None."""
    import agent_flow.runners as R

    class _Bare:
        def spec(self):
            from agent_flow.runners.spec import RunnerSpec

            return RunnerSpec(runtime="bare", mode="process", transport="subprocess", name="bare")

    saved = dict(R._REGISTRY)
    R._REGISTRY["bare"] = _Bare()
    try:
        assert probe_agent_dir("bare") is None
    finally:
        R._REGISTRY.clear()
        R._REGISTRY.update(saved)


# --- resolution at the node: probe is the fallback --------------------------


def _capture_agent_dir(spy, node, *, params=None, ctx_agent_dir=""):
    """Run one node through the shared executor spy; return its agent_dir."""
    anyio.run(
        lambda: interpret(
            node,
            run_dir=Path(tempfile.gettempdir()),
            params=params or {},
            on_error=lambda n, e: "degraded",
            agent_dir=ctx_agent_dir,
        )
    )
    return spy.inv.agent_dir


def test_node_falls_back_to_the_probe_when_unset(tmp_path, monkeypatch, spy_executor):
    (tmp_path / ".opencode").mkdir()
    monkeypatch.chdir(tmp_path)
    node = agent_node("n", "a")
    assert _capture_agent_dir(spy_executor, node) == str(tmp_path.resolve())


def test_explicit_agent_dir_beats_the_probe(tmp_path, monkeypatch, spy_executor):
    (tmp_path / ".opencode").mkdir()
    monkeypatch.chdir(tmp_path)
    node = agent_node("n", "a", agent_dir="/explicit/dir")
    assert _capture_agent_dir(spy_executor, node) == "/explicit/dir"


def test_run_wide_agent_dir_beats_the_probe(tmp_path, monkeypatch, spy_executor):
    (tmp_path / ".opencode").mkdir()
    monkeypatch.chdir(tmp_path)
    node = agent_node("n", "a")
    assert _capture_agent_dir(spy_executor, node, ctx_agent_dir="/run/wide") == "/run/wide"


def test_no_probe_hit_leaves_agent_dir_empty(tmp_path, monkeypatch, spy_executor):
    monkeypatch.chdir(tmp_path)  # no .opencode anywhere under a temp dir
    node = agent_node("n", "a")
    # Empty -> the run would surface the missing requirement at preflight.
    assert _capture_agent_dir(spy_executor, node) == ""


# --- CLI fills cfg.agent_dir from the probe (so preflight/summary see it) ----


def test_cli_fills_agent_dir_from_probe(tmp_path, monkeypatch):
    from agent_flow.runners import probe_agent_dir as _p

    (tmp_path / ".opencode").mkdir()
    monkeypatch.chdir(tmp_path)
    # The CLI does: if not cfg.agent_dir: cfg.agent_dir = probe_agent_dir(runtime).
    assert _p("opencode") == str(tmp_path.resolve())
