"""Unit tests for env.load_env — .env loading with real-env-wins precedence."""

import os

from agent_flow.core.env import load_env


def test_load_env_missing_file_is_noop(tmp_path):
    assert load_env(tmp_path / "nope.env") == []


def test_load_env_none_is_noop():
    assert load_env(None) == []


def test_load_env_adds_unset_vars(tmp_path, monkeypatch):
    monkeypatch.delenv("AF_TEST_A", raising=False)
    env = tmp_path / ".env"
    env.write_text("AF_TEST_A=from-file\n")
    added = load_env(env)
    assert added == ["AF_TEST_A"]
    assert os.environ["AF_TEST_A"] == "from-file"


def test_load_env_does_not_override_real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("AF_TEST_B", "from-shell")
    env = tmp_path / ".env"
    env.write_text("AF_TEST_B=from-file\n")
    added = load_env(env)
    assert added == []  # already set -> not contributed
    assert os.environ["AF_TEST_B"] == "from-shell"  # real env wins
