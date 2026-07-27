"""Unit tests for path utilities (run_dir resolution + temp base)."""

from pathlib import Path

import pytest

from agent_flow.utils import default_temp_base, resolve_run_dir, resolve_template


def test_resolve_template_substitutes_params():
    out = resolve_template("{repos_root}/{product_key}/cloud-assessment/_agent-flow", {"repos_root": "/r", "product_key": "demo"})
    assert out == "/r/demo/cloud-assessment/_agent-flow"


def test_resolve_template_lenient_leaves_missing_key_literal():
    # Default (lenient): an unknown placeholder must not crash — left as-is.
    assert resolve_template("{repos_root}/{unknown}", {"repos_root": "/r"}) == "{repos_root}/{unknown}"


def test_resolve_template_strict_raises_on_missing_key():
    # strict=True (paths): a missing placeholder is a hard error.
    with pytest.raises(KeyError):
        resolve_template("{repos_root}/{unknown}", {"repos_root": "/r"}, strict=True)


def test_resolve_template_empty_passthrough():
    assert resolve_template("", {"x": "1"}) == ""


@pytest.mark.parametrize(
    "value",
    [
        "a } b",  # stray closing brace (ValueError) — e.g. an agent's prose/JSON
        "a { b",  # stray opening brace (ValueError)
        "{p:!!!}",  # malformed format spec (ValueError)
        "{p.nope}",  # attribute that does not exist (AttributeError)
        "{}",  # empty field with no positional args (IndexError)
        r"match \d{2,3}",  # a regex quantifier (KeyError)
    ],
)
def test_resolve_template_lenient_never_raises_on_free_text(value):
    """Lenient mode must return non-template text UNCHANGED, never raise.

    Regression: only KeyError/IndexError were caught, so a single stray brace —
    routine in prose, JSON/code fragments, and an agent's own `reason` surfaced
    by a gate as a re-run instruction — raised ValueError and aborted the run.
    """
    assert resolve_template(value, {"p": "s"}) == value


def test_resolve_template_strict_still_raises_on_malformed():
    # strict (paths) must NOT swallow a malformed template — it is always a bug there.
    with pytest.raises(ValueError):
        resolve_template("a } b", {}, strict=True)


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
