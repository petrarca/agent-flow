"""Shared rich Console — a single process-wide instance for all CLI output."""

from __future__ import annotations


def get_console():
    """Return a shared rich Console (lazily; requires the `cli` extra)."""
    from rich.console import Console

    global _CONSOLE
    try:
        return _CONSOLE
    except NameError:
        _CONSOLE = Console()
        return _CONSOLE
