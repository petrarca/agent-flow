"""Unit tests for opencode event parsing — liveness/telemetry only, plus raw line.

We deliberately do NOT parse events into summaries (that would couple us to the
runner's schema). parse_event extracts only what supervision needs (tokens, cost,
is_terminal) and keeps the original line in `raw` for optional display.
"""

import json

from agent_flow.runners import OpenCodeRunner


def test_step_finish_carries_telemetry_and_terminal():
    line = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 1200}, "cost": 0.02, "reason": "stop"}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.is_event
    assert ev.is_terminal
    assert ev.tokens == 1200
    assert ev.cost == 0.02
    assert ev.raw  # original line preserved for display


def test_step_finish_non_stop_is_not_terminal():
    line = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 300}, "reason": "tool-calls"}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.is_terminal is False
    assert ev.tokens == 300


def test_tool_event_is_heartbeat_with_raw_only():
    line = json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "write", "state": {"status": "completed"}}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.is_event  # heartbeat
    assert ev.tokens == 0
    assert ev.raw == line  # verbatim, no interpretation


def test_non_json_is_not_an_event():
    ev = OpenCodeRunner().parse_event("some log noise")
    assert ev.is_event is False


# The one-line display projection lives in the CLI layer (display concern), not
# the runner/engine. It must be tolerant and never raise on unknown shapes.


def test_project_tool_event_shows_tool_and_target():
    from agent_flow.cli import _project_event

    raw = json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "write", "state": {"input": {"filePath": "/x/out.md"}}}})
    line = _project_event(raw)
    assert "write" in line and "/x/out.md" in line


def test_project_step_finish_shows_tokens():
    from agent_flow.cli import _project_event

    raw = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 12793}}})
    assert "12,793" in _project_event(raw)


def test_project_text_event_shows_message():
    from agent_flow.cli import _project_event

    raw = json.dumps({"type": "text", "part": {"type": "text", "text": "  hello   world  "}})
    assert "hello world" in _project_event(raw)


def test_project_non_json_is_trimmed_passthrough():
    from agent_flow.cli import _project_event

    assert _project_event("plain log line") == "plain log line"
