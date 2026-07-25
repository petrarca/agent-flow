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
    from agent_flow.cli.events import _project_event

    raw = json.dumps({"type": "tool_use", "part": {"type": "tool", "tool": "write", "state": {"input": {"filePath": "/x/out.md"}}}})
    line = _project_event(raw)
    assert "write" in line and "/x/out.md" in line


def test_project_step_finish_shows_tokens():
    from agent_flow.cli.events import _project_event

    raw = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 12793}}})
    assert "12,793" in _project_event(raw)


def test_project_text_event_shows_message():
    from agent_flow.cli.events import _project_event

    raw = json.dumps({"type": "text", "part": {"type": "text", "text": "  hello   world  "}})
    assert "hello world" in _project_event(raw)


def test_project_non_json_is_trimmed_passthrough():
    from agent_flow.cli.events import _project_event

    assert _project_event("plain log line") == "plain log line"


# The runner normalizes each event into NEUTRAL display fields (kind/title/
# detail/status). The CLI renders only those — it never re-parses opencode JSON.


def test_neutral_view_step_end_carries_kind_and_tokens():
    line = json.dumps({"type": "step_finish", "part": {"type": "step-finish", "tokens": {"total": 1200}, "reason": "stop"}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.kind == "step_end"
    assert ev.tokens == 1200
    assert ev.is_terminal


def test_neutral_view_tool_prefers_state_title():
    line = json.dumps({"part": {"type": "tool", "tool": "edit", "state": {"title": "Edit src/app.py", "status": "completed"}}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.kind == "tool"
    assert ev.title == "Edit src/app.py"  # opencode's own summary, not just the path
    assert ev.status == "completed"


def test_neutral_view_tool_falls_back_to_input_target():
    line = json.dumps({"part": {"type": "tool", "tool": "edit", "state": {"input": {"filePath": "/x/app.py"}}}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.title == "edit /x/app.py"  # no title -> "<tool> <target>"


def test_neutral_view_tool_name_prefixed_when_title_is_bare_target():
    # opencode's state.title is inconsistent: for 'read' it is often just the
    # bare path (no verb). We must still lead with the tool name so every line
    # reads uniformly ("read <path>"), not a bare path with no tool.
    line = json.dumps({"part": {"type": "tool", "tool": "read", "state": {"title": "/x/cloud-readiness.md"}}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.title == "read /x/cloud-readiness.md"


def test_neutral_view_tool_name_not_doubled_when_title_has_verb():
    # When state.title already starts with the tool verb (edit -> "Edit <file>"),
    # do NOT prefix again ("edit Edit ...").
    line = json.dumps({"part": {"type": "tool", "tool": "edit", "state": {"title": "Edit app.py"}}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.title == "Edit app.py"


def test_neutral_view_edit_carries_structured_diff_counts():
    # The runner supplies STRUCTURED counts (added/removed) + the diff text; it
    # does NOT format "+A/-D" (that's the CLI's job).
    line = json.dumps(
        {
            "part": {
                "type": "tool",
                "tool": "edit",
                "state": {"title": "Edit x.md", "metadata": {"diff": "@@ -1 +1,2 @@\n-a\n+b\n+c", "filediff": {"additions": 12, "deletions": 3}}},
            }
        }
    )
    ev = OpenCodeRunner().parse_event(line)
    assert ev.added == 12 and ev.removed == 3
    assert "@@" in ev.diff  # the unified diff text is carried neutrally
    assert ev.detail == ""  # no formatted diff stat baked into detail


def test_render_event_formats_diff_stat_from_counts():
    from agent_flow.cli import render_event
    from agent_flow.runners import Event

    out = render_event(Event(kind="tool", title="Edit x.md", status="completed", added=12, removed=3))
    assert "+12/-3" in out


def test_render_diff_renders_the_patch(capsys):
    from rich.console import Console

    from agent_flow.cli import render_diff
    from agent_flow.runners import Event

    console = Console(force_terminal=False)
    render_diff(Event(kind="tool", diff="@@ -1 +1 @@\n-old\n+new"), console=console)
    out = capsys.readouterr().out
    assert "old" in out and "new" in out


def test_render_diff_noop_without_diff(capsys):
    from rich.console import Console

    from agent_flow.cli import render_diff
    from agent_flow.runners import Event

    console = Console(force_terminal=False)
    render_diff(Event(kind="tool", diff=""), console=console)
    assert capsys.readouterr().out == ""


def test_diff_rows_strips_header_and_pairs_changes():
    from agent_flow.cli.events import _diff_rows

    diff = "Index: x\n===\n--- /a/x\n+++ /a/x\n@@ -1,2 +1,2 @@\n context\n-old line\n+new line"
    rows = _diff_rows(diff)
    kinds = [r[0] for r in rows]
    # header noise (Index/===/---/+++) is dropped; @@ -> hdr, then ctx, then chg
    assert kinds == ["hdr", "ctx", "chg"]
    assert rows[0][1].startswith("@@")
    assert rows[1][1] == "context" and rows[1][2] == ""  # context: same text is set on left only here
    assert rows[2][1] == "old line" and rows[2][2] == "new line"  # change: removal|addition paired


def test_diff_rows_unbalanced_change_pads():
    from agent_flow.cli.events import _diff_rows

    # two removals, one addition -> the extra removal pairs with an empty right
    diff = "@@ -1,2 +1,1 @@\n-a\n-b\n+c"
    rows = [r for r in _diff_rows(diff) if r[0] == "chg"]
    assert rows == [("chg", "a", "c"), ("chg", "b", "")]


def test_render_diff_unified_is_default(capsys):
    # default style is "unified": one column with "- "/"+ " sign lines.
    from rich.console import Console

    from agent_flow.cli import render_diff
    from agent_flow.runners import Event

    console = Console(force_terminal=False, width=80)
    render_diff(Event(kind="tool", diff="@@ -1 +1 @@\n-old\n+new"), console=console)
    out = capsys.readouterr().out
    assert "- old" in out and "+ new" in out


def test_render_diff_split_two_columns(capsys):
    from rich.console import Console

    from agent_flow.cli import render_diff
    from agent_flow.runners import Event

    console = Console(force_terminal=False, width=80)
    render_diff(Event(kind="tool", diff="@@ -1 +1 @@\n-old\n+new"), console=console, style="split")
    out = capsys.readouterr().out
    # both old and new appear, side by side on the same line
    assert "old" in out and "new" in out
    assert any("old" in ln and "new" in ln for ln in out.splitlines())


def test_render_diff_is_bracketed_with_filename_label(capsys):
    # the block is framed by top/bottom hairline rules; the top rule carries the
    # file (from ev.title, since the diff's own filename header is stripped).
    from rich.console import Console

    from agent_flow.cli import render_diff
    from agent_flow.runners import Event

    console = Console(force_terminal=False, width=80)
    render_diff(Event(kind="tool", title="Edit app.py", diff="@@ -1 +1 @@\n-old\n+new"), console=console)
    out = capsys.readouterr().out
    assert "Edit app.py" in out  # filename label on the top rule
    assert "\u2500" in out  # hairline rule character


def test_neutral_view_tool_metadata_hint_and_error_status():
    line = json.dumps({"part": {"type": "tool", "tool": "grep", "state": {"title": "Grep foo", "metadata": {"matches": 12}, "error": "boom"}}})
    ev = OpenCodeRunner().parse_event(line)
    assert ev.detail == "12 matches"
    assert ev.status == "error"


def test_render_event_tool_colors_by_status():
    from agent_flow.cli import render_event
    from agent_flow.runners import Event

    assert "[green]" in render_event(Event(kind="tool", title="Edit x", status="completed"))
    assert "[red]" in render_event(Event(kind="tool", title="Edit x", status="error"))
    assert "[cyan]" in render_event(Event(kind="tool", title="Edit x", status="running"))


def test_render_event_tool_shows_detail():
    from agent_flow.cli import render_event
    from agent_flow.runners import Event

    out = render_event(Event(kind="tool", title="Grep foo", detail="12 matches", status="completed"))
    assert "Grep foo" in out and "12 matches" in out
