"""`--config` assembly — path OR inline JSON, repeatable, deep-merged.

Stage D: a run config can be given as a file path, as inline JSON, or as several
of either; later sources deep-merge over earlier so a patch tweaks one key of a
dict-valued setting (durations/nodes/options) instead of replacing the whole map.
"""

import pytest

from agent_flow.run_config import _assemble_config, _deep_merge, _is_inline_json, build_run_config

# --- the primitives ---------------------------------------------------------


def test_is_inline_json_detects_an_object():
    assert _is_inline_json('{"a": 1}')
    assert _is_inline_json('   {"a": 1}')  # leading space tolerated
    assert not _is_inline_json("run.yml")
    assert not _is_inline_json("path/to/config.yaml")


def test_deep_merge_recurses_into_dicts():
    base = {"durations": {"short": 60, "long": 600}, "runtime": "opencode"}
    overlay = {"durations": {"long": 900}, "model": "m"}
    assert _deep_merge(base, overlay) == {
        "durations": {"short": 60, "long": 900},  # long retuned, short kept
        "runtime": "opencode",
        "model": "m",
    }


def test_deep_merge_replaces_scalars_and_lists():
    assert _deep_merge({"a": [1, 2]}, {"a": [3]}) == {"a": [3]}  # lists replace, not concat
    assert _deep_merge({"a": 1}, {"a": 2}) == {"a": 2}


def test_deep_merge_is_pure():
    base = {"d": {"x": 1}}
    _deep_merge(base, {"d": {"y": 2}})
    assert base == {"d": {"x": 1}}  # unchanged


# --- assembly from files + inline -------------------------------------------


def test_inline_json_source(tmp_path):
    cfg = build_run_config(config_file='{"runtime": "mock", "model": "prov/x"}')
    assert cfg.runtime == "mock"
    assert cfg.model == "prov/x"


def test_file_source(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text("runtime: mock\nmodel: prov/y\n")
    cfg = build_run_config(config_file=str(p))
    assert cfg.model == "prov/y"


def test_file_then_inline_patch_deep_merges(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text("runtime: mock\ndurations: {short: 60, long: 600}\n")
    # A second source patches ONE duration; the rest of the map survives.
    cfg = build_run_config(config_file=[str(p), '{"durations": {"long": 900}}'])
    assert cfg.durations == {"short": 60, "long": 900}


def test_later_source_wins_on_a_scalar(tmp_path):
    cfg = build_run_config(config_file=['{"model": "first"}', '{"model": "second"}'])
    assert cfg.model == "second"


def test_nodes_map_deep_merges_per_node(tmp_path):
    base = '{"nodes": {"a": {"model": "m1"}, "b": {"model": "m2"}}}'
    patch = '{"nodes": {"a": {"duration": "long"}}}'
    cfg = build_run_config(config_file=[base, patch])
    # a gains duration AND keeps its model; b untouched.
    assert cfg.nodes["a"].model == "m1"
    assert cfg.nodes["a"].duration == "long"
    assert cfg.nodes["b"].model == "m2"


def test_options_map_deep_merges(tmp_path):
    cfg = build_run_config(config_file=['{"options": {"serve_url": "u", "keep": 1}}', '{"options": {"serve_url": "v"}}'])
    assert cfg.options == {"serve_url": "v", "keep": 1}


# --- errors -----------------------------------------------------------------


def test_missing_file_raises():
    with pytest.raises(FileNotFoundError, match="not found"):
        build_run_config(config_file="/no/such/config.yml")


def test_unknown_key_in_any_layer_fails(tmp_path):
    p = tmp_path / "run.yml"
    p.write_text("runtime: mock\n")
    with pytest.raises(ValueError, match="unknown run config keys"):
        build_run_config(config_file=[str(p), '{"bogus": 1}'])


def test_non_mapping_inline_rejected():
    with pytest.raises(ValueError, match="must be a mapping"):
        _assemble_config(["[1, 2, 3]"])


def test_empty_yaml_file_is_harmless(tmp_path):
    p = tmp_path / "empty.yml"
    p.write_text("")
    cfg = build_run_config(config_file=str(p))
    assert cfg.runtime == "opencode"  # falls back to the default


# --- precedence: CLI/env still beat --config --------------------------------


def test_cli_override_beats_config():
    # build_run_config's explicit kwargs are the CLI tier and must win over --config.
    cfg = build_run_config(config_file='{"model": "from-config"}', model="from-cli")
    assert cfg.model == "from-cli"
