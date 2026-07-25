"""Shared rich Console — a single process-wide instance for all CLI output."""

from __future__ import annotations


def get_console():
    """Return a shared rich Console (lazily; requires the `cli` extra).

    rich is an optional dependency (the `cli` extra). All CLI rendering funnels
    through this console, so guarding the import here gives one clear install
    message rather than a bare ModuleNotFoundError from a deep render call.
    """
    from agent_flow.utils import require_extra

    console_mod = require_extra("rich.console", "cli", "CLI output rendering")

    global _CONSOLE
    try:
        return _CONSOLE
    except NameError:
        _CONSOLE = console_mod.Console()
        return _CONSOLE
