"""Unit tests for path utilities (run_dir resolution + temp base)."""

from pathlib import Path

from agent_flow.utils import default_temp_base, resolve_run_dir


def test_explicit_run_dir_is_used_verbatim(tmp_path):
    given = tmp_path / "out"
    assert resolve_run_dir(str(given)) == given.resolve()


def test_unset_run_dir_goes_under_temp_base_agent_flow():
    d = resolve_run_dir("", name="my-flow")
    base = default_temp_base() / "agent-flow"
    assert str(d).startswith(str(base))
    assert d.is_dir()  # created
    assert "my-flow-" in d.name  # name slug present


def test_unset_run_dir_is_unique_per_call():
    a = resolve_run_dir("", name="x")
    b = resolve_run_dir("", name="x")
    assert a != b  # each call -> a distinct dir (no cross-run collision)


def test_default_temp_base_is_a_dir_parent():
    # POSIX -> /tmp; Windows -> the OS temp dir. Either way, a usable base.
    base = default_temp_base()
    assert isinstance(base, Path)
    assert base.is_absolute()
