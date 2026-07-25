"""Node-lifecycle progress view — the default line-based per-node display.

`NodeProgressPrinter.on_node_event` consumes the engine's on_node_event data
(start/finish, status, agent label, elapsed) and prints one line per transition.
"""

from __future__ import annotations

import time

from agent_flow.cli.console import get_console


class NodeProgressPrinter:
    """Line-based node progress — no cursor games, safe with logs and threads.

    Consumes the engine's on_node_event hook and prints ONE line per node
    transition (running / ok / degraded / failed). Deliberately NOT a repainting
    Live/TUI: those fight Prefect's threaded task execution and interleaved
    logging. A plain `console.print` per event interleaves cleanly and never
    corrupts the terminal.

    Consumers who want a richer view (a Live table, a TUI) can build one on the
    SAME hooks (on_node_event + on_event_factory) — this class is just the simple
    default. The end-of-run results table is printed separately by run_cli.
    """

    _MARK = {
        "running": ("cyan", ">"),
        "ok": ("green", "check"),
        "verified": ("green", "check"),
        "degraded": ("yellow", "warn"),
        "failed": ("red", "x"),
    }

    def __init__(self, *, console=None):
        self._console = console or get_console()
        self._started: dict[str, float] = {}  # node name -> start monotonic time

    def on_node_event(self, name: str, phase: str, status: str | None, agent: str) -> None:
        """Print a status line for a node start/finish transition.

        Shows the agent as informal context and, on finish, the elapsed time
        (timed here; the authoritative duration also rides on the flow's
        NodeOutcome and appears in the end-of-run results table).
        """
        state = "running" if phase == "start" else (status or "ok")
        style, mark = self._MARK.get(state, ("white", "-"))
        agent_s = f" [dim]({agent})[/dim]" if agent else ""
        if phase == "start":
            self._started[name] = time.monotonic()
            dur_s = ""
        else:
            start = self._started.pop(name, None)
            dur_s = f" [dim]{time.monotonic() - start:.1f}s[/dim]" if start is not None else ""
        self._console.print(f"[{style}]{mark:>5}[/{style}] {name} [{style}]{state}[/{style}]{agent_s}{dur_s}")
