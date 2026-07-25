"""Unit tests for the runtime pre-flight checks.

The generic module (`preflight`) owns only runtime-agnostic checks
(`check_prefect_importable`, `check_agent_dir_exists`). Runtime-specific
checks (opencode on PATH, not-nested session, `.opencode/agent*` layout) live
on the runner via `preflight_checks(agent_dir)` and reach `check()` through
that seam — so they are exercised here via `check("opencode", ...)`, not as
standalone functions.
"""

import pytest

from agent_flow import preflight


@pytest.fixture
def agent_dir(tmp_path):
    """A valid agent-definitions dir: has .opencode/agent/."""
    (tmp_path / ".opencode" / "agent").mkdir(parents=True)
    return tmp_path


# --- generic checks ---------------------------------------------------------


def test_agent_dir_exists_ok(agent_dir):
    c = preflight.check_agent_dir_exists(agent_dir)
    assert c.ok and c.fatal


def test_agent_dir_exists_missing_path(tmp_path):
    c = preflight.check_agent_dir_exists(tmp_path / "nope")
    assert not c.ok and c.fatal


def test_agent_dir_exists_none():
    c = preflight.check_agent_dir_exists(None)
    assert not c.ok and c.fatal


def test_agent_dir_exists_is_layout_agnostic(tmp_path):
    # The generic check only verifies the dir is set and exists; the opencode
    # LAYOUT (.opencode/agent*) is the runner's concern, not this one.
    c = preflight.check_agent_dir_exists(tmp_path)
    assert c.ok and c.fatal


def test_prefect_importable():
    assert preflight.check_prefect_importable().ok


# --- runtime seam: check() delegates runtime checks to the runner -----------


def test_check_mock_skips_opencode_specific_checks(agent_dir, monkeypatch):
    # Even inside an opencode session, the mock runtime contributes no
    # opencode-installed / not-nested / layout checks (MockRunner has no
    # preflight_checks). Only the generic checks run.
    monkeypatch.setenv("OPENCODE", "1")
    names = {c.name for c in preflight.check("mock", agent_dir)}
    assert "opencode-installed" not in names
    assert "not-nested-session" not in names
    assert "agent-dir" in names


def test_check_local_backend_omits_prefect(agent_dir):
    # Default (local) backend: no prefect-importable check — a local run must not
    # fail merely because Prefect is absent.
    names = {c.name for c in preflight.check("mock", agent_dir)}
    assert "prefect-importable" not in names


def test_check_prefect_backend_includes_prefect(agent_dir):
    # Selecting the prefect backend adds the prefect-importable check.
    names = {c.name for c in preflight.check("mock", agent_dir, backend="prefect")}
    assert "prefect-importable" in names


def test_check_opencode_includes_runtime_checks(agent_dir, monkeypatch):
    monkeypatch.delenv("OPENCODE", raising=False)
    names = {c.name for c in preflight.check("opencode", agent_dir)}
    # generic (agent-dir) + runner-contributed opencode checks; no prefect (local)
    assert {"agent-dir", "opencode-installed", "not-nested-session"} <= names
    assert "prefect-importable" not in names


def test_check_opencode_not_nested_fails_inside_session(agent_dir, monkeypatch):
    monkeypatch.setenv("OPENCODE", "1")
    checks = {c.name: c for c in preflight.check("opencode", agent_dir)}
    assert not checks["not-nested-session"].ok
    assert checks["not-nested-session"].fatal


def test_check_opencode_layout_fails_without_agent_dir(tmp_path, monkeypatch):
    # agent_dir exists but has no .opencode/agent* -> the runner's layout check fails.
    monkeypatch.delenv("OPENCODE", raising=False)
    checks = {c.name: c for c in preflight.check("opencode", tmp_path)}
    assert not checks["opencode-agent-layout"].ok


def test_check_unknown_runtime_runs_only_generic_checks(agent_dir):
    # An unknown runtime contributes no runner checks; generic ones still run.
    names = {c.name for c in preflight.check("does-not-exist", agent_dir)}
    assert names == {"agent-dir"}


def test_fatal_failures_filters(agent_dir, monkeypatch):
    monkeypatch.setenv("OPENCODE", "1")
    results = preflight.check("opencode", agent_dir / "nope")
    failures = preflight.fatal_failures(results)
    names = {c.name for c in failures}
    # bad agent-dir + not-nested-session are both fatal failures here
    assert "agent-dir" in names
    assert "not-nested-session" in names
    assert all(c.fatal and not c.ok for c in failures)
