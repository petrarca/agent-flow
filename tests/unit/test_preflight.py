"""Unit tests for the runtime pre-flight checks."""

import pytest

from agent_flow import preflight


@pytest.fixture
def agent_dir(tmp_path):
    """A valid agent-definitions dir: has .opencode/agent/."""
    (tmp_path / ".opencode" / "agent").mkdir(parents=True)
    return tmp_path


def test_agent_dir_ok(agent_dir):
    c = preflight.check_agent_dir(agent_dir)
    assert c.ok and c.fatal


def test_agent_dir_missing_path(tmp_path):
    c = preflight.check_agent_dir(tmp_path / "nope")
    assert not c.ok and c.fatal


def test_agent_dir_none():
    c = preflight.check_agent_dir(None)
    assert not c.ok and c.fatal


def test_agent_dir_without_opencode(tmp_path):
    # exists but no .opencode/agent* inside
    c = preflight.check_agent_dir(tmp_path)
    assert not c.ok


def test_not_nested_session_pass(monkeypatch):
    monkeypatch.delenv("OPENCODE", raising=False)
    assert preflight.check_not_nested_session().ok


def test_not_nested_session_fail(monkeypatch):
    monkeypatch.setenv("OPENCODE", "1")
    c = preflight.check_not_nested_session()
    assert not c.ok and c.fatal


def test_prefect_importable():
    assert preflight.check_prefect_importable().ok


def test_check_mock_skips_opencode_specific_checks(agent_dir, monkeypatch):
    # Even inside an opencode session, the mock runtime does not run the
    # opencode-installed / not-nested checks.
    monkeypatch.setenv("OPENCODE", "1")
    names = {c.name for c in preflight.check("mock", agent_dir)}
    assert "opencode-installed" not in names
    assert "not-nested-session" not in names
    assert {"prefect-importable", "agent-dir"} <= names


def test_check_opencode_includes_runtime_checks(agent_dir, monkeypatch):
    monkeypatch.delenv("OPENCODE", raising=False)
    names = {c.name for c in preflight.check("opencode", agent_dir)}
    assert {"opencode-installed", "not-nested-session"} <= names


def test_fatal_failures_filters(agent_dir, monkeypatch):
    monkeypatch.setenv("OPENCODE", "1")
    results = preflight.check("opencode", tmp_bad := (agent_dir / "nope"))
    failures = preflight.fatal_failures(results)
    # agent-dir (bad path) + not-nested-session are fatal failures here
    names = {c.name for c in failures}
    assert "agent-dir" in names
    assert "not-nested-session" in names
    assert all(c.fatal and not c.ok for c in failures)
    _ = tmp_bad
