"""Unit tests for the AgentRunner strategy (command building + event parsing)."""

import json
from pathlib import Path

import pytest

from agent_flow.runners import (
    AgentInvocation,
    OpenCodeRunner,
    get_runner,
)


def _inv(**kw) -> AgentInvocation:
    """AgentInvocation with a filled run_dir — these tests exercise only
    build_command, which does not use run_dir, so a placeholder is fine."""
    kw.setdefault("run_dir", Path("/tmp/run"))
    return AgentInvocation(**kw)


def test_get_runner_known():
    assert get_runner("opencode").name == "opencode"


def test_get_runner_unknown_raises():
    with pytest.raises(ValueError):
        get_runner("does-not-exist")


def test_mock_is_not_a_runner():
    # Mock is a MODE (--mock-agents), not a runtime; it must not be in RUNNERS.
    with pytest.raises(ValueError):
        get_runner("mock")


def test_opencode_build_command_shape():
    r = OpenCodeRunner()
    cmd = r.build_command(_inv(agent="tech-stack-analyst", prompt="PRODUCT_KEY: x", model="m")).argv
    assert cmd[:2] == ["opencode", "run"]
    assert "--agent" in cmd and "tech-stack-analyst" in cmd
    assert "--model" in cmd and "m" in cmd
    assert "--format" in cmd and "json" in cmd
    assert "--auto" in cmd  # headless: auto-approve non-denied permissions
    assert cmd[-1] == "PRODUCT_KEY: x"  # prompt is the trailing positional


def test_opencode_build_command_display_elides_prompt():
    # The LaunchSpec.display is diagnosis-safe: flags shown, prompt elided.
    spec = OpenCodeRunner().build_command(_inv(agent="a", prompt="SECRET-LONG-PROMPT", model="m"))
    assert "opencode run --agent a" in spec.display
    assert "SECRET-LONG-PROMPT" not in spec.display
    assert "<prompt:" in spec.display


def test_opencode_build_command_omits_model_when_unset():
    # No model configured -> NO --model flag, so the runtime (opencode) resolves
    # the model from its own config. The library never hardcodes a model.
    cmd = OpenCodeRunner().build_command(_inv(agent="a", prompt="p")).argv
    assert "--model" not in cmd


def test_opencode_build_command_emits_dir_when_agent_dir_set():
    cmd = OpenCodeRunner().build_command(_inv(agent="a", prompt="p", agent_dir="/proj")).argv
    assert "--dir" in cmd and "/proj" in cmd
    assert cmd.index("--dir") < cmd.index("/proj")
    assert cmd[-1] == "p"  # prompt still trailing


def test_opencode_build_command_no_dir_when_agent_dir_absent():
    cmd = OpenCodeRunner().build_command(_inv(agent="a", prompt="p")).argv
    assert "--dir" not in cmd


def test_opencode_parse_event_step_finish_terminal():
    # Real shape: telemetry is on a part with type "step-finish"; reason "stop" is terminal.
    line = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 1234}, "cost": 0.0, "reason": "stop"}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.is_event is True
    assert ev.tokens == 1234
    assert ev.is_terminal is True


def test_opencode_parse_event_step_finish_nonterminal():
    line = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 10}, "reason": "tool-calls"}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.tokens == 10
    assert ev.is_terminal is False


def test_opencode_parse_event_nonjson_is_not_event():
    ev = OpenCodeRunner().parse_event("just some text")
    assert ev.is_event is False


def test_opencode_parse_event_other_event_type_is_heartbeat():
    ev = OpenCodeRunner().parse_event(json.dumps({"type": "tool_use", "part": {}}))
    assert ev.is_event is True
    assert ev.tokens == 0


def test_opencode_parse_event_surfaces_runtime_error():
    # opencode emits {"type":"error", error:{name, data:{message, ref}}} on stdout
    # and exits non-zero without a sidecar; the runner must surface it as ev.error.
    line = json.dumps({"type": "error", "error": {"name": "UnknownError", "data": {"message": "Unexpected server error.", "ref": "err_abc123"}}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.is_event is True
    assert ev.kind == "error"
    assert "UnknownError" in ev.error
    assert "Unexpected server error" in ev.error
    assert "err_abc123" in ev.error
