"""Unit tests for the run-config settings (RunConfig pydantic-settings + params)."""

import pytest

from agent_flow.run_config import (
    RunConfig,
    build_run_config,
    clear_settings,
    get_settings,
    init_settings,
    parse_params,
)

_ENV_KEYS = (
    "AGENT_FLOW_RUNTIME",
    "AGENT_FLOW_RUN_DIR",
    "AGENT_FLOW_AGENT_DIR",
    "AGENT_FLOW_LLM_CONCURRENCY",
    "AGENT_FLOW_INSTRUCTIONS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate each test from ambient AGENT_FLOW_* env and the settings singleton."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)
    clear_settings()
    yield
    clear_settings()


# --- parse_params -----------------------------------------------------------


def test_parse_params_basic():
    assert parse_params(["product_key=my-product", "repos_root=/tmp/repos"]) == {
        "product_key": "my-product",
        "repos_root": "/tmp/repos",
    }


def test_parse_params_none_and_empty_value():
    assert parse_params(None) == {}
    assert parse_params(["k="]) == {"k": ""}


def test_parse_params_missing_eq_raises():
    with pytest.raises(ValueError, match="KEY=VALUE"):
        parse_params(["product_key"])


def test_parse_params_empty_key_raises():
    with pytest.raises(ValueError, match="empty key"):
        parse_params(["=value"])


# --- RunConfig defaults + precedence ---------------------------------------


def test_defaults():
    cfg = build_run_config()
    assert cfg.runtime == "opencode"
    assert cfg.run_dir == ""
    assert cfg.llm_concurrency is None
    assert cfg.show_events is False


def test_cli_override_wins():
    assert build_run_config(runtime="mock").runtime == "mock"


def test_none_cli_override_dropped_so_env_wins(monkeypatch):
    monkeypatch.setenv("AGENT_FLOW_RUNTIME", "fromenv")
    # A None CLI value must not clobber the env value.
    assert build_run_config(runtime=None).runtime == "fromenv"


def test_cli_beats_env(monkeypatch):
    monkeypatch.setenv("AGENT_FLOW_RUNTIME", "fromenv")
    assert build_run_config(runtime="fromcli").runtime == "fromcli"


def test_yaml_config_lowest_explicit_source(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text("runtime: fromyaml\nllm_concurrency: 3\nparams:\n  product_key: x\n")
    cfg = build_run_config(config_file=str(p))
    assert cfg.runtime == "fromyaml"
    assert cfg.llm_concurrency == 3


def test_env_beats_yaml(tmp_path, monkeypatch):
    p = tmp_path / "run.yml"
    p.write_text("runtime: fromyaml\n")
    monkeypatch.setenv("AGENT_FLOW_RUNTIME", "fromenv")
    assert build_run_config(config_file=str(p)).runtime == "fromenv"


def test_yaml_nodes_section(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text(
        "runtime: mock\n"
        "nodes:\n"
        '  analyst: {instructions: "full breakdown", model: "prov/big"}\n'
        '  summary: {instructions: "lead with tenancy", duration: long}\n'
    )
    cfg = build_run_config(config_file=str(p))
    assert cfg.nodes["analyst"].instructions == "full breakdown"
    assert cfg.nodes["analyst"].model == "prov/big"
    assert cfg.nodes["summary"].duration == "long"


def test_yaml_rejects_unknown_key(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text("runtimee: opencode\n")  # typo
    with pytest.raises(ValueError, match="unknown config keys"):
        build_run_config(config_file=str(p))


def test_yaml_rejects_non_mapping(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text("- a\n- b\n")
    with pytest.raises(ValueError, match="must be a mapping"):
        build_run_config(config_file=str(p))


def test_resolved_instructions_prefers_file(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("from file")
    cfg = RunConfig(instructions="inline", instructions_file=str(brief))
    assert cfg.resolved_instructions() == "from file"
    assert RunConfig(instructions="inline").resolved_instructions() == "inline"


# --- settings lifecycle (lru_cache singleton) ------------------------------


def test_get_settings_singleton_stable():
    assert get_settings() is get_settings()


def test_init_settings_installs_override():
    init_settings(runtime="mock")
    assert get_settings().runtime == "mock"
    assert get_settings() is get_settings()


def test_clear_settings_resets():
    init_settings(runtime="mock")
    assert get_settings().runtime == "mock"
    clear_settings()
    # After clear, a fresh RunConfig from defaults/env (runtime default here).
    assert get_settings().runtime == "opencode"
