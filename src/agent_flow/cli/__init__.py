"""CLI package — human-facing output + the reusable run command.

The library core stays render-agnostic (it emits neutral `Event`s /
`on_node_event` data and returns `NodeOutcome`s); this package turns those into
terminal output with rich/typer. Split by concern:

  - console.py  — the shared rich Console (`get_console`).
  - events.py   — live event/diff rendering (`event_printer`, `render_event`,
                  `render_diff`).
  - progress.py — the default per-node progress view (`NodeProgressPrinter`).
  - tables.py   — end-of-run tables (`print_results_table`,
                  `print_preflight_results`).
  - app.py      — `run_cli`: builds the multi-command Typer app and wires each
                  command module (cli/commands/) via register(app, ctx).
  - commands/   — one module per command: run.py (`run`), flow.py (`flow`).

The public names are re-exported here so `from agent_flow.cli import ...` (and
`from agent_flow import ...`) stay stable regardless of the internal layout.
"""

from __future__ import annotations

from agent_flow.cli.app import run_cli
from agent_flow.cli.console import get_console
from agent_flow.cli.events import event_printer, render_diff, render_event
from agent_flow.cli.progress import NodeProgressPrinter
from agent_flow.cli.tables import print_preflight_results, print_results_table

__all__ = [
    "get_console",
    "event_printer",
    "render_event",
    "render_diff",
    "NodeProgressPrinter",
    "print_results_table",
    "print_preflight_results",
    "run_cli",
]
