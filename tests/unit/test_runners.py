"""Unit tests for the AgentRunner strategy (command building + event parsing)."""

import json

import pytest

from agent_flow.runners import (
    AgentInvocation,
    MockRunner,
    OpenCodeRunner,
    get_runner,
)


def test_get_runner_known():
    assert get_runner("opencode").name == "opencode"
    assert get_runner("mock").name == "mock"


def test_get_runner_unknown_raises():
    with pytest.raises(ValueError):
        get_runner("does-not-exist")


def test_opencode_build_command_shape():
    r = OpenCodeRunner()
    cmd = r.build_command(AgentInvocation(agent="tech-stack-analyst", prompt="PRODUCT_KEY: x", model="m"))
    assert cmd[:2] == ["opencode", "run"]
    assert "--agent" in cmd and "tech-stack-analyst" in cmd
    assert "--model" in cmd and "m" in cmd
    assert "--format" in cmd and "json" in cmd
    assert "--auto" in cmd  # headless: auto-approve non-denied permissions
    assert cmd[-1] == "PRODUCT_KEY: x"  # prompt is the trailing positional


def test_opencode_build_command_omits_model_when_unset():
    # No model configured -> NO --model flag, so the runtime (opencode) resolves
    # the model from its own config. The library never hardcodes a model.
    cmd = OpenCodeRunner().build_command(AgentInvocation(agent="a", prompt="p"))
    assert "--model" not in cmd


def test_mock_build_command_omits_model_when_unset():
    cmd = MockRunner().build_command(AgentInvocation(agent="a", prompt="p"))
    assert "--model" not in cmd


def test_opencode_build_command_emits_dir_when_agent_dir_set():
    cmd = OpenCodeRunner().build_command(AgentInvocation(agent="a", prompt="p", agent_dir="/proj"))
    assert "--dir" in cmd and "/proj" in cmd
    assert cmd.index("--dir") < cmd.index("/proj")
    assert cmd[-1] == "p"  # prompt still trailing


def test_opencode_build_command_no_dir_when_agent_dir_absent():
    cmd = OpenCodeRunner().build_command(AgentInvocation(agent="a", prompt="p"))
    assert "--dir" not in cmd


def test_mock_ignores_agent_dir():
    cmd = MockRunner().build_command(AgentInvocation(agent="analyst", prompt="p", agent_dir="/proj"))
    assert "--dir" not in cmd  # mock has no project concept


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


def test_mock_runner_command_and_no_events():
    r = MockRunner()
    cmd = r.build_command(AgentInvocation(agent="domain-analyst", prompt="p"))
    assert cmd[0] == "python3"
    assert "--agent" in cmd and "domain-analyst" in cmd
    assert r.parse_event("anything").is_event is False
