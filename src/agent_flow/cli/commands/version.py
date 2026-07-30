"""The `version` command — the pipeline's version(s).

Two layers, following the client/server convention (kubectl / docker / terraform):
the CONSUMER's own app version is primary, and the agent-flow framework version
that powers it is shown as a secondary layer. The consumer supplies its version
via `run_cli(version=...)`; when omitted, only the agent-flow version is shown.

    run_cli(version="0.3.1")  ->  "cloud-readiness 0.3.1 (agent-flow 0.1.2)"
    run_cli()                 ->  "cloud-readiness (agent-flow 0.1.2)"

agent-flow's own version comes from `agent_flow.__version__` (the installed
distribution version, set by setuptools-scm at build time).
"""

from __future__ import annotations

from agent_flow.cli.console import get_console
from agent_flow.cli.context import RunCliContext


def register(app, ctx: RunCliContext) -> None:
    """Attach the top-level `version` command to `app`."""

    @app.command("version")
    def version() -> None:
        """Print the pipeline version(s): consumer app + agent-flow."""
        from agent_flow._version import __version__

        app_version = f" {ctx.version}" if ctx.version else ""
        get_console().print(f"{ctx.name}{app_version} (agent-flow {__version__})")
