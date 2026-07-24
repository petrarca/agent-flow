"""Unit tests for the run-config protocol (config file + --param + precedence)."""

import pytest

from agent_flow.run_config import RunConfig, load_run_config, merge, parse_params


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


def test_load_run_config_sections(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text(
        "runtime: opencode\n"
        "run_dir: '{repos_root}/{product_key}/output'\n"
        "agent_dir: /work/pipeline\n"
        "llm_concurrency: 2\n"
        "instructions: use code-graph\n"
        "params:\n"
        "  product_key: my-product\n"
        "  repos_root: /tmp/repos\n"
    )
    cfg = load_run_config(p)
    assert cfg.runtime == "opencode"
    assert cfg.run_dir == "{repos_root}/{product_key}/output"
    assert cfg.agent_dir == "/work/pipeline"
    assert cfg.llm_concurrency == 2
    assert cfg.instructions == "use code-graph"
    assert cfg.params == {"product_key": "my-product", "repos_root": "/tmp/repos"}


def test_load_run_config_rejects_unknown_key(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text("runtimee: opencode\n")  # typo
    with pytest.raises(ValueError, match="unknown config keys"):
        load_run_config(p)


def test_load_run_config_rejects_non_mapping_params(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text("params:\n  - a\n  - b\n")
    with pytest.raises(ValueError, match="`params` must be a mapping"):
        load_run_config(p)


def test_merge_cli_over_file():
    base = RunConfig(runtime="opencode", run_dir="fromfile", params={"product_key": "x", "keep": "y"})
    merged = merge(
        base,
        cli_overrides={"runtime": "mock", "run_dir": None, "agent_dir": None},
        cli_params={"product_key": "override"},
    )
    assert merged.runtime == "mock"  # CLI wins
    assert merged.run_dir == "fromfile"  # None override -> file value kept
    assert merged.params == {"product_key": "override", "keep": "y"}  # per-key merge


def test_resolved_instructions_prefers_file(tmp_path):
    brief = tmp_path / "brief.md"
    brief.write_text("from file")
    cfg = RunConfig(instructions="inline", instructions_file=str(brief))
    assert cfg.resolved_instructions() == "from file"
    assert RunConfig(instructions="inline").resolved_instructions() == "inline"
