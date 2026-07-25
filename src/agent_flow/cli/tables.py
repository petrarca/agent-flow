"""End-of-run tables — results (node/agent/outcome/duration) and pre-flight checks."""

from __future__ import annotations

from agent_flow.cli.console import get_console


def print_results_table(results, *, title: str = "Pipeline results", agents: dict[str, str] | None = None, console=None) -> None:
    """Print an end-of-run node -> outcome table (agent label + per-node duration).

    `results` maps node name -> NodeOutcome (status + duration_s). A bare status
    string is also accepted (duration blank) so older callers still work.
    `agents` optionally maps node name -> agent label for an informal Agent
    column; omitted -> no Agent column.
    """
    from rich.table import Table

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
        style = {"ok": "green", "degraded": "yellow"}.get(status, "white")
        dur = f"{duration:.1f}s" if isinstance(duration, (int, float)) else ""
        row = [name] + ([agents.get(name, "")] if show_agent else []) + [f"[{style}]{status}[/{style}]", dur]
        table.add_row(*row)
    console.print(table)


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
