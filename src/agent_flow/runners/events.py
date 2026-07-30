"""`Event` — the neutral, runtime-agnostic view of one thing an agent did.

A runner's `parse_event` translates its runtime's wire format into this shape at
the boundary, so nothing downstream re-parses vendor JSON: the supervisor reads
the liveness/telemetry fields, the CLI reads the display fields.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    """One parsed unit of runner stdout: liveness + telemetry + a NEUTRAL view.

    Two audiences, cleanly separated so nothing above the runner knows the
    runtime's wire format:

    - SUPERVISION (engine): tokens / cost / is_terminal / is_event. Unchanged.

    - DISPLAY (CLI): a runner-AGNOSTIC, already-normalized view of the event —
      `kind` + `title` + `detail` + `status`. The RUNNER fills these from its own
      (versioned, runtime-specific) schema; the CLI renders them with styling and
      never re-parses `raw`. This is the seam: "how to read the stream" lives in
      the runner, "how to lay it out" lives in the CLI, and they meet on these
      neutral fields. A new runtime (Claude Code, …) populates the same fields
      from its own stream and the existing CLI renders it with zero changes.

    - DIAGNOSTIC: `raw` still carries the ORIGINAL stdout line verbatim, for
      --show-events deep debugging and for runtimes whose renderer wants it. It
      is a passthrough, NOT something the neutral renderer interprets.

    kind    neutral category the CLI styles: "step_start" | "step_end" | "tool" |
            "text" | "other" ("" when this is not a displayable event).
    title   primary human summary (tool title/target, the message text, …).
    detail  secondary hint (tool metadata: "12 matches", "exit 0", a diff stat).
    status  tool lifecycle for coloring: "running" | "completed" | "error" | "".
    For a file-changing tool (edit/write), three neutral, structured fields carry
    the change — the runner MAPS its runtime's native shape onto them (it does not
    parse or compute): opencode `metadata.diff` + `filediff.{additions,deletions}`,
    Claude Code `gitDiff.{patch,additions,deletions}`. The CLI is a dumb renderer:
    it formats `+added/-removed` for the one-line detail and renders `diff` as a
    syntax-highlighted block only under --show-diffs.

    diff    unified-diff patch text (or "").
    added   lines added (0 if none/unknown).
    removed lines removed (0 if none/unknown).
    """

    tokens: int = 0
    cost: float = 0.0
    is_terminal: bool = False
    is_event: bool = True  # False for lines that are not real events (ignored for liveness)
    raw: str = ""  # the original stdout line, diagnostic passthrough (NOT for neutral render)
    # A RUNTIME ERROR the runner recognised in the stream (e.g. opencode's
    # {"type":"error"} line: model unresolved, provider failure). "" when the
    # event is not an error. The supervisor collects these so that a run which
    # ends with no control sidecar can report WHY, not just "no sidecar written".
    error: str = ""
    # Neutral display view (runner-filled, CLI-rendered) — see class docstring.
    kind: str = ""
    title: str = ""
    detail: str = ""
    status: str = ""
    diff: str = ""
    added: int = 0
    removed: int = 0

    @staticmethod
    def none() -> Event:
        return Event(is_event=False)
