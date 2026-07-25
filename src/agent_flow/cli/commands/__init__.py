"""CLI command modules.

Each module exposes `register(app, ctx)` (see cli/context.RunCliContext) that
attaches its command(s) to the shared Typer app. run_cli builds the app and
calls each register — the petrarca noun/verb subcommand style, adapted to a
reusable CLI factory whose app cannot be a module-level singleton.
"""
