"""Unit tests for utils.require_extra (the optional-dependency guard)."""

import pytest

from agent_flow.utils import require_extra


def test_require_extra_returns_module_when_present():
    # A stdlib module always imports -> returned as-is.
    mod = require_extra("json", "cli", "some feature")
    assert mod.dumps({"a": 1}) == '{"a": 1}'


def test_require_extra_raises_friendly_on_missing():
    with pytest.raises(ImportError) as exc:
        require_extra("no_such_module_xyz", "cli", "the run_cli command")
    msg = str(exc.value)
    assert "the run_cli command" in msg
    assert "pip install 'agent-flow[cli]'" in msg


def test_require_extra_names_the_extra():
    with pytest.raises(ImportError, match=r"agent-flow\[prefect\]"):
        require_extra("no_such_module_xyz", "prefect", "the Prefect backend")
