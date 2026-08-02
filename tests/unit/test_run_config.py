"""Unit tests for the run-config settings (RunConfig pydantic-settings + params)."""

import pytest

from agent_flow.run_config import RunConfig, build_run_config, parse_params

_ENV_KEYS = (
    "AGENT_FLOW_RUNTIME",
    "AGENT_FLOW_RUN_DIR",
    "AGENT_FLOW_AGENT_DIR",
    "AGENT_FLOW_LLM_CONCURRENCY",
    "AGENT_FLOW_INSTRUCTIONS",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """Isolate each test from ambient AGENT_FLOW_* env."""
    for k in _ENV_KEYS:
        monkeypatch.delenv(k, raising=False)


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
    with pytest.raises(ValueError, match="unknown run config keys"):
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


# --- run_config= shapes: defaults vs already-resolved -----------------------
#
# There is deliberately NO settings singleton: each run holds its own RunConfig,
# threaded explicitly. A consumer building their own CLI resolves one with
# build_run_config(...) and hands it over; it must then be honoured verbatim.


def test_a_dict_run_config_is_a_base_layer_that_env_overrides(monkeypatch):
    monkeypatch.setenv("AGENT_FLOW_RUNTIME", "from-env")
    cfg = build_run_config(base={"runtime": "my-default"})
    assert cfg.runtime == "from-env", "a dict means 'my defaults' — env wins"


def test_a_runconfig_instance_is_already_resolved(monkeypatch):
    """A RunConfig is a BaseSettings: env was applied at construction and the
    caller's explicit value already beat it. Re-resolving would apply env twice
    and demote the caller — so run_flow honours the instance verbatim."""
    monkeypatch.setenv("AGENT_FLOW_MODEL", "from-env")
    cfg = build_run_config(model="from-my-cli")
    assert cfg.model == "from-my-cli"

    from agent_flow import FlowDef, FlowRegistry, NodeDef
    from agent_flow.flowdef.compile import _build_pipeline_and_call

    flow = FlowDef(name="t", nodes=[NodeDef(name="n", agent="a")])
    _, call = _build_pipeline_and_call(flow, FlowRegistry(), run_dir="", start_from="", only="", stop_after="", params={}, run_config=cfg)
    assert call["model"] == "from-my-cli", "a passed RunConfig must not be re-resolved"


def test_no_settings_singleton_is_exported():
    import agent_flow

    for gone in ("get_settings", "init_settings", "clear_settings"):
        assert not hasattr(agent_flow, gone), f"{gone} should be removed"
