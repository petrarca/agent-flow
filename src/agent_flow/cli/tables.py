"""End-of-run tables — results (node/agent/outcome/duration) and pre-flight checks."""

from __future__ import annotations

from agent_flow.cli.console import get_console


def print_results_table(
    results, *, title: str = "Pipeline results", agents: dict[str, str] | None = None, console=None, elapsed_s: float | None = None
) -> None:
    """Print an end-of-run node -> outcome table (agent label + per-node duration).

    `results` maps node name -> NodeOutcome (status + duration_s + runtime). A
    bare status string is also accepted (duration/runtime blank) so older callers
    still work. `agents` optionally maps node name -> agent name; when present,
    the Agent column shows the RUNTIME-QUALIFIED label "<runtime>:<agent>" (e.g.
    "opencode:my-agent", "inproc:some-agent", "mock:other-agent"), the runtime
    taken from each NodeOutcome. Omit `agents` -> no Agent column.

    `elapsed_s` is the run's WALL-CLOCK duration; when given it is rendered as a
    separated Total row. Wall clock is deliberately NOT the sum of the per-node
    durations: nodes in a parallel group overlap, so the sum overstates what you
    actually waited (and a jump-back re-runs a node, counting it twice). The
    per-node column stays the time each node took; the Total is the run.
    """
    from rich.table import Table

    from agent_flow.runners.executor import qualified_agent

    console = console or get_console()
    agents = agents or {}
    show_agent = any(agents.values())
    table = Table(title=title, title_style="bold")
    table.add_column("Node")
    if show_agent:
        table.add_column("Agent", style="dim")
    table.add_column("Outcome")
    table.add_column("Duration", justify="right")
    # Outcomes are "ok" or "degraded" — a blocking failure raises NodeBlocked
    # rather than appearing here, so there is no "blocked" status to color.
    for name, outcome in results.items():
        status = getattr(outcome, "status", outcome)
        duration = getattr(outcome, "duration_s", None)
        runtime = getattr(outcome, "runtime", "")
        style = {"ok": "green", "degraded": "yellow"}.get(status, "white")
        dur = f"{duration:.1f}s" if isinstance(duration, (int, float)) else ""
        label = qualified_agent(runtime, agents.get(name, ""))
        row = [name] + ([label] if show_agent else []) + [f"[{style}]{status}[/{style}]", dur]
        table.add_row(*row)
    if isinstance(elapsed_s, (int, float)):
        # A separated summary row: the run's wall clock (see the docstring — not
        # the sum of the node durations, which double-counts parallel work).
        table.add_section()
        total = [f"[bold]Total ({len(results)} nodes)[/bold]"] + ([""] if show_agent else []) + ["", f"[bold]{_human(elapsed_s)}[/bold]"]
        table.add_row(*total)
    console.print(table)


def _human(seconds: float) -> str:
    """Compact wall-clock: `42.3s` under a minute, else `12m 03s` / `1h 04m 12s`."""
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def print_preflight_results(results, *, title: str = "Pre-flight checks", console=None) -> None:
    """Print pre-flight `Check` results as simple status lines (house style).

    Purely generic: it iterates whatever checks it is given and derives the
    status marker/style from each Check's `ok`/`fatal` flags — no check names,
    counts, or per-runtime knowledge are baked in. New checks (or a future
    runtime's own checks) render automatically.

    One line per check, aligned, with a leading marker:
      ok=True                -> "check" (green)  — no detail (nothing to explain)
      ok=False, fatal=True   -> "fail"  (red)    + detail — a reason not to start
      ok=False, fatal=False  -> "warn"  (yellow) + detail — non-blocking
    """
    console = console or get_console()
    console.print(f"[bold]{title}[/bold]")
    width = max((len(c.name) for c in results), default=0)
    for c in results:
        if c.ok:
            style, label, detail = "green", "check", ""
        elif c.fatal:
            style, label, detail = "red", "fail", f" — {c.detail}"
        else:
            style, label, detail = "yellow", "warn", f" — {c.detail}"
        console.print(f"  [{style}]{label:>5}[/{style}] {c.name:<{width}}{detail}")
