"""Unit tests for context ingestion (read files -> injected prompt content)."""

from agent_flow.context import read_context_blocks


def test_reads_file_content_with_header(tmp_path):
    (tmp_path / "rules.md").write_text("Always X.")
    out = read_context_blocks(["rules.md"], params={}, run_dir=tmp_path)
    assert "### Context: rules.md" in out
    assert "Always X." in out


def test_empty_or_none_sources(tmp_path):
    assert read_context_blocks(None, params={}, run_dir=tmp_path) == ""
    assert read_context_blocks([], params={}, run_dir=tmp_path) == ""


def test_missing_source_warns_and_skips(tmp_path):
    warnings = []
    out = read_context_blocks(["nope.md"], params={}, run_dir=tmp_path, warn=warnings.append)
    assert out == ""
    assert warnings and "nope.md" in warnings[0]


def test_templating_against_params(tmp_path):
    (tmp_path / "acme.md").write_text("acme rules")
    out = read_context_blocks(["{product_key}.md"], params={"product_key": "acme"}, run_dir=tmp_path)
    assert "acme rules" in out


def test_run_dir_template_and_absolute_equivalent(tmp_path):
    (tmp_path / "r.md").write_text("R")
    bare = read_context_blocks(["r.md"], params={}, run_dir=tmp_path)
    prefixed = read_context_blocks(["{run_dir}/r.md"], params={}, run_dir=tmp_path)
    assert "R" in bare and "R" in prefixed


def test_glob_reads_all_matches_sorted(tmp_path):
    (tmp_path / "a.md").write_text("AAA")
    (tmp_path / "b.md").write_text("BBB")
    out = read_context_blocks(["*.md"], params={}, run_dir=tmp_path)
    assert out.index("AAA") < out.index("BBB")  # sorted


def test_multiple_sources_concatenated(tmp_path):
    (tmp_path / "one.md").write_text("ONE")
    (tmp_path / "two.md").write_text("TWO")
    out = read_context_blocks(["one.md", "two.md"], params={}, run_dir=tmp_path)
    assert out.index("ONE") < out.index("TWO")
