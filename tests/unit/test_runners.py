"""Unit tests for the AgentRunner strategy (command building + event parsing)."""

import json
from pathlib import Path

import pytest

from agent_flow.runners import (
    MODE_PROCESS,
    TRANSPORT_SUBPROCESS,
    AgentInvocation,
    OpenCodeRunner,
    get_runner,
    register,
    runner_specs,
)
from agent_flow.runners.base import RunnerSpec


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
    # Mock is a MODE (--mock-agents), not a runtime; it must not be registered.
    with pytest.raises(ValueError):
        get_runner("mock")


# --- RunnerSpec + registry ----------------------------------------------------


def test_opencode_spec():
    spec = get_runner("opencode").spec()
    assert spec.runtime == "opencode"
    assert spec.mode == MODE_PROCESS
    assert spec.transport == TRANSPORT_SUBPROCESS
    assert spec.name == "opencode"
    assert spec.needs_endpoint is False


def test_opencode_build_verdict_preamble_matches_control_preamble():
    # The runner's verdict preamble IS the shared sidecar preamble — opencode
    # delegates to build_control_preamble, so the output must be identical.
    from agent_flow.protocol import build_control_preamble

    runner = OpenCodeRunner()
    schema = {"type": "object", "properties": {"answer": {"type": "string"}}}
    got = runner.build_verdict_preamble("my-agent", "/tmp/run/my-node.control.json", schema)
    expected = build_control_preamble("my-agent", "/tmp/run/my-node.control.json", schema)
    assert got == expected
    assert "CONTROL_FILE: /tmp/run/my-node.control.json" in got
    assert "my-agent" in got


def test_opencode_build_verdict_preamble_no_schema():
    runner = OpenCodeRunner()
    got = runner.build_verdict_preamble("a", "/tmp/x.control.json")
    assert "CONTROL_FILE: /tmp/x.control.json" in got


def test_runner_specs_dedup():
    # runner_specs() returns one spec per distinct runner (aliases collapse).
    names = [s.name for s in runner_specs()]
    assert "opencode" in names
    assert len(names) == len(set(names))  # no duplicate primary names


class _DummyRunner:
    """A minimal runner with aliases, for registry tests."""

    def spec(self) -> RunnerSpec:
        return RunnerSpec(
            runtime="dummy",
            mode=MODE_PROCESS,
            transport=TRANSPORT_SUBPROCESS,
            name="dummy",
            aliases=("dummy-alias",),
        )

    def parse_event(self, raw):  # pragma: no cover - not exercised here
        raise NotImplementedError

    def build_command(self, inv):  # pragma: no cover
        raise NotImplementedError


def test_register_indexes_name_and_aliases():
    runner = register(_DummyRunner())
    try:
        assert get_runner("dummy") is runner
        assert get_runner("dummy-alias") is runner
    finally:
        # clean up the global registry so other tests are unaffected
        from agent_flow.runners import _REGISTRY

        _REGISTRY.pop("dummy", None)
        _REGISTRY.pop("dummy-alias", None)


def test_register_duplicate_name_raises():
    register(_DummyRunner())
    try:
        with pytest.raises(ValueError):
            register(_DummyRunner())  # "dummy" already registered
    finally:
        from agent_flow.runners import _REGISTRY

        _REGISTRY.pop("dummy", None)
        _REGISTRY.pop("dummy-alias", None)


def test_opencode_build_command_shape():
    r = OpenCodeRunner()
    spec = r.build_command(_inv(agent="tech-stack-analyst", prompt="PRODUCT_KEY: x", model="m"))
    cmd = spec.argv
    assert cmd[:2] == ["opencode", "run"]
    assert "--agent" in cmd and "tech-stack-analyst" in cmd
    assert "--model" in cmd and "m" in cmd
    assert "--format" in cmd and "json" in cmd
    assert "--auto" in cmd  # headless: auto-approve non-denied permissions
    assert "--print-logs" in cmd  # stderr capture: real error messages on failure
    assert "--log-level" in cmd and cmd[cmd.index("--log-level") + 1] == "ERROR"
    assert cmd[-1] == "PRODUCT_KEY: x"  # prompt is the trailing positional
    assert spec.capture_stderr is True  # stderr on separate pipe


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


# --- parse_stderr_line tests --------------------------------------------------


def test_parse_stderr_line_extracts_error_and_ref():
    # The actionable shape: level=ERROR message=failed ref=... error="..."
    line = (
        "timestamp=2026-07-26T14:15:07.115Z level=ERROR run=abc message=failed"
        ' ref=err_0ddf08a6 error="ProviderModelNotFoundError: Model not found: bla/."'
    )
    result = OpenCodeRunner().parse_stderr_line(line)
    assert result is not None
    assert "ProviderModelNotFoundError" in result
    assert "ref err_0ddf08a6" in result


def test_parse_stderr_line_bare_error_without_ref():
    line = "timestamp=2026-07-26T14:15:07.115Z level=ERROR run=abc message=failed error=SomeError:whatever"
    result = OpenCodeRunner().parse_stderr_line(line)
    assert result == "SomeError:whatever"


def test_parse_stderr_line_ignores_no_error_field():
    # Secondary ERROR lines that have no error= field (e.g. "share subscriber failed")
    line = 'timestamp=2026-07-26T14:15:07.109Z level=ERROR run=abc message="share subscriber failed" type=message.updated cause="Cause([Fail(...)])"'
    result = OpenCodeRunner().parse_stderr_line(line)
    assert result is None


def test_parse_stderr_line_ignores_non_error_level():
    line = 'timestamp=2026-07-26T14:15:06.885Z level=INFO run=abc message="creating instance" directory=/tmp/x'
    result = OpenCodeRunner().parse_stderr_line(line)
    assert result is None


def test_parse_stderr_line_returns_none_for_empty():
    assert OpenCodeRunner().parse_stderr_line("") is None


# --- LaunchSpec.capture_stderr default ----------------------------------------


def test_launch_spec_capture_stderr_default_false():
    from agent_flow.runners.base import LaunchSpec

    spec = LaunchSpec(argv=["opencode", "run"], display="opencode run ...")
    assert spec.capture_stderr is False
