"""Live event rendering — turn a runner's neutral Event stream into terminal output.

`event_printer(...)` returns an on_event callback; `render_event` formats one
event as a line, `render_diff` renders edit/write diffs (unified or side-by-side).
The core stays render-agnostic (runners fill neutral Event fields); this module
only lays them out. rich is imported lazily inside the functions.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from agent_flow.cli.console import get_console

if TYPE_CHECKING:
    from agent_flow.runners import Event


def event_printer(label: str, *, console=None, lines: bool = True, diffs: bool = False, diff_style: str = "unified") -> Callable[[Event], None]:
    """Build an `on_event` callback that renders live events.

    `label` is the prefix each line carries. The engine passes the NODE name (the
    DAG unit the reader navigates by), not the agent that implements it — in a
    firehose of live lines the node is what tells you where in the flow you are.

    Two INDEPENDENT, composable outputs (from --show-events / --show-diffs):
      - `lines`  : one styled progress line per event (the firehose).
      - `diffs`  : for a file-changing event carrying a diff, a rendered diff
                   block (unified or split, per `diff_style`) AFTER its line.
                   Works with or without `lines`, so `--show-diffs` alone layers
                   diffs onto the default table view.

    Rendering is RUNNER-AGNOSTIC: the runner already normalized the event into
    neutral fields (kind/title/detail/status/diff) in `parse_event`; here we only
    map those to this CLI's palette. The CLI never re-parses the runtime's wire
    format — that knowledge lives in the runner.

    Usage:
        run_agent(..., on_event=event_printer("analyst", diffs=True))
    """
    console = console or get_console()

    def _print(ev: Event) -> None:
        if lines:
            line = render_event(ev)
            if line:
                console.print(f"  [dim]{label}[/dim] {line}")
        if diffs and ev.diff:
            render_diff(ev, console=console, style=diff_style)

    return _print


def render_diff(ev: Event, *, console=None, style: str = "unified") -> None:
    """Render a file-change event's diff. `style` is the user's choice:

      - "unified"  : one column, red removals / green additions, header stripped
                     (OpenCode's headless look; robust on any terminal width).
      - "split"    : side-by-side two columns (old | new), 50/50, for wide
                     terminals and large edits.

    Rich has no native diff widget; both are built rich-only by parsing the diff
    (shared `_diff_rows`) and laying it out. We strip HEADER noise
    (Index:/===/---/+++) since the tool line already names the file. The block is
    bracketed by a thin top rule LABELLED with the file (from `ev.title`, since we
    stripped the diff's own filename header) and a plain bottom rule, so it stands
    out from surrounding log lines. Reads only neutral `ev` fields. Never raises —
    falls back to a plain colored block.
    """
    if not ev.diff:
        return
    console = console or get_console()
    try:
        from rich.rule import Rule

        label = ev.title or "diff"
        console.print(Rule(f"[dim]{label}[/dim]", style="dim", characters="\u2500"))
        if style == "split":
            _render_side_by_side(ev.diff, console)
        else:
            _render_unified(ev.diff, console)
        console.print(Rule(style="dim", characters="\u2500"))
    except Exception:  # noqa: BLE001 - display must never break a run
        from rich.syntax import Syntax

        console.print(Syntax(ev.diff, "diff", theme="ansi_dark", background_color="default", word_wrap=True))


def _render_unified(diff: str, console) -> None:
    """One-column diff: header stripped, magenta hunks, red/green change lines."""
    from rich.text import Text

    for kind, left, right in _diff_rows(diff):
        if kind == "hdr":
            console.print(Text(left, style="magenta"))
        elif kind == "ctx":
            console.print(Text("  " + left, style="dim"))
        else:  # chg — emit removal then addition, sign-colored
            if left:
                console.print(Text("- " + left, style="red"))
            if right:
                console.print(Text("+ " + right, style="green"))


def _diff_rows(diff: str) -> list[tuple[str, str, str]]:
    """Parse a unified diff into aligned (kind, old, new) rows for a 2-col view.

    kind is "ctx" | "chg" (change) | "hdr" (hunk @@ header). Header noise
    (Index:/===/--- /+++ ) is dropped. Consecutive '-'/'+' lines are paired
    positionally (removal left, addition right); unbalanced ones pad with "".
    """
    rows: list[tuple[str, str, str]] = []
    removed: list[str] = []
    added: list[str] = []

    def flush() -> None:
        for i in range(max(len(removed), len(added))):
            rows.append(("chg", removed[i] if i < len(removed) else "", added[i] if i < len(added) else ""))
        removed.clear()
        added.clear()

    for line in diff.splitlines():
        if line.startswith(("Index:", "===", "--- ", "+++ ", "diff --git")):
            continue  # header noise — the tool line already names the file
        if line.startswith("@@"):
            flush()
            rows.append(("hdr", line, ""))
        elif line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        else:
            flush()
            rows.append(("ctx", line[1:] if line.startswith(" ") else line, ""))
    flush()
    return rows


def _render_side_by_side(diff: str, console) -> None:
    """Render parsed diff rows as a borderless two-column rich Table."""
    from rich.table import Table
    from rich.text import Text

    rows = _diff_rows(diff)
    if not rows:
        return
    table = Table(show_header=False, show_edge=False, box=None, padding=(0, 1), expand=True)
    table.add_column(ratio=1, overflow="fold")
    table.add_column(ratio=1, overflow="fold")
    for kind, left, right in rows:
        if kind == "hdr":
            table.add_row(Text(left, style="magenta"), Text(""))
        elif kind == "ctx":
            table.add_row(Text(left, style="dim"), Text(left, style="dim"))
        else:  # chg — color alone carries add/remove (no -/+ glyph clutter)
            lt = Text(left, style="red") if left else Text("")
            rt = Text(right, style="green") if right else Text("")
            table.add_row(lt, rt)
    console.print(table)


def render_event(ev: Event) -> str:
    """Style one NEUTRAL event into a rich display line (this CLI's palette).

    The runner owns classification (ev.kind/status) and content (ev.title/detail);
    this owns COLOR. `kind` drives the base style; for tools, `status` refines it
    (running=cyan, completed=green, error=red) — a richer coloring than a flat
    hue, made possible by the neutral status field.

    We deliberately color only the LEADING keyword ("tool"/"step done") and leave
    the CONTENT (ev.title — file paths, patterns, URLs) UNSTYLED. rich's default
    highlighter then auto-colors those tokens (paths magenta, numbers, quoted
    strings, …). Wrapping the whole line in an explicit style would suppress that,
    so the keyword carries our semantic status color and rich decorates the rest.

    Content is shown in FULL (rich soft-wraps); --show-events is a debug stream
    where seeing the whole line matters more than fitting one physical row.

    Never raises: an unknown kind falls back to the (already trimmed) title.
    """
    kind = ev.kind
    if kind == "step_start":
        return "[dim]- step[/dim]"
    if kind == "step_end":
        return f"[green]step done[/green] [dim]({ev.tokens:,} tokens)[/dim]" if ev.tokens else "[green]step done[/green]"
    if kind == "tool":
        style = {"error": "red", "completed": "green"}.get(ev.status, "cyan")
        # Prefer a diff stat (+A/-D) built here from the neutral counts; else the
        # runner's non-diff hint (matches / exit code). Formatting lives in the
        # CLI — the runner only supplies numbers.
        hint = f"+{ev.added}/-{ev.removed}" if (ev.added or ev.removed) else ev.detail
        detail = f" [dim]({hint})[/dim]" if hint else ""
        # Color the keyword by status; leave the title bare for rich to highlight.
        return f"[{style}]tool[/{style}] {ev.title}{detail}".rstrip()
    if kind == "text":
        return f"[white]{ev.title}[/white]" if ev.title else ""
    # "other" / unknown: show the title (the event type) dimmed, if any.
    return f"[dim]{ev.title}[/dim]" if ev.title else ""


def _project_event(raw: str) -> str:
    """Back-compat shim: render a raw opencode line via the neutral path.

    The opencode wire-shape knowledge now lives in OpenCodeRunner.parse_event;
    this only re-parses a raw line through it and styles the result, so callers
    (and tests) that still pass a raw string keep working. The LIVE path uses the
    runner-filled Event directly and never touches this.
    """
    from agent_flow.runners import OpenCodeRunner

    raw = raw.strip()
    if not raw.startswith("{"):
        return raw
    ev = OpenCodeRunner().parse_event(raw)
    return render_event(ev) if ev.is_event else raw
