"""run_cli — assemble the pipeline's Typer app from command modules.

`run_cli(build_nodes, ...)` is the reusable CLI a pipeline author calls. It
builds a multi-command Typer app and wires each command module's `register(app,
ctx)` onto it (see cli/commands/). Because the app is parameterized by the
consumer's `build_nodes` / `params_model`, it is built per call (not a
module-level singleton) — so commands receive their config via a `RunCliContext`
rather than importing a global.

Commands today:
  - `run`     — execute the pipeline (all the generic run flags + -p/--param).
  - `flow`    — inspect the pipeline's flow / graph (`flow nodes`).
  - `version` — print the pipeline name + the agent-flow version.

The rendering helpers (event_printer, NodeProgressPrinter, print_results_table,
print_preflight_results, get_console) live in the sibling cli modules and are
re-exported from `agent_flow.cli`. typer/rich are the optional `cli` extra,
imported lazily so the core stays install-light.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_flow.engine import Node


def run_cli(
    flow: Callable[[], list[Node]] | object,
    *,
    name: str = "agent-flow",
    llm_tag: str = "llm",
    default_agent_dir: str = "",
    default_run_dir: str = "",
    params_model: type | None = None,
    registry: object = None,
    version: str | None = None,
) -> None:
    """Run a pipeline from a UNIFIED CLI: subcommands over generic flags + params.

    A pipeline author calls this with a `build_nodes()` callable (returns the
    Node list). The app exposes subcommands:
      - `run`   — execute the pipeline. Generic run settings as flags
        (--runtime/--backend/--run-dir/--agent-dir/--instructions/--show-events/
        --start-from/--only/--llm-concurrency/--model/--idle-timeout), --config
        <run.yml> for the same settings (lowest source), and repeatable
        -p/--param KEY=VALUE for DOMAIN params. Precedence: CLI flag > env
        (AGENT_FLOW_*) > .env > --config YAML > default.
      - `flow nodes` — the pipeline's nodes in execution order (name, agent,
        group, deps), to discover --only/--start-from targets.
      - `version` — the pipeline name, the consumer's app version (the `version`
        argument, if given), and the agent-flow version powering it.

    `version` is the CONSUMER's own app version string (e.g. its package
    version). It is shown as the primary version; agent-flow's version is always
    shown as a secondary layer (the two-layer client/server convention). Omit it
    and only the agent-flow version is shown.

    `default_agent_dir` / `default_run_dir` supply the pipeline's own fallbacks
    when neither CLI nor env set them; `default_run_dir` may use `{param}`
    templating, resolved strictly at run time.

    Domain params (`params_model`):
      - None (default): -p values pass through as an untyped string dict.
      - a pydantic-settings BaseSettings subclass: -p become init kwargs
        (highest) over bare env / .env / defaults; VALIDATED (required fields,
        DirectoryPath, Literal, validators), a failure aborts with exit 2 before
        any agent spawns. The validated values are dumped back to the `params`
        dict, so downstream {name} templating is unchanged either way.

    The selected backend owns bootstrap/teardown (inside build_flow); the caller
    does not bootstrap Prefect. The default backend is inprocess (no Prefect);
    pass --backend prefect to opt in.

    Requires the `cli` extra (typer + rich): pip install 'petrarca-agent-flow[cli]'.
    """
    from agent_flow.utils import require_extra

    typer = require_extra("typer", "cli", "the run_cli command")

    from agent_flow.cli.commands import flow as flow_cmd
    from agent_flow.cli.commands import run as run_cmd
    from agent_flow.cli.commands import version as version_cmd
    from agent_flow.cli.context import RunCliContext

    # `flow` is either a FlowDef (the declarative surface) or a build_nodes()
    # callable (the lower-level form). A FlowDef supplies build_nodes (compile it
    # against the registry) and default name/backend; the registry defaults to a
    # built-ins-only FlowRegistry when none was passed.
    from agent_flow.flowdef import FlowDef, compile_flow

    if isinstance(flow, FlowDef):
        if registry is None:
            from agent_flow.registry import FlowRegistry

            registry = FlowRegistry()
        flow_def = flow
        reg = registry
        build_nodes = lambda: compile_flow(flow_def, reg)  # noqa: E731
        if name == "agent-flow":
            name = flow_def.name
        # The FlowDef's flow-level agent_dir is the pipeline's own default (used
        # unless the CLI/env/config sets --agent-dir). Other flow-level fields
        # (backend, shared_*, llm_concurrency) are honored via the CLI flags/env
        # on the run command; the FlowDef values act as documentation there.
        if not default_agent_dir and flow_def.agent_dir:
            default_agent_dir = flow_def.agent_dir
    else:
        build_nodes = flow

    ctx = RunCliContext(
        build_nodes=build_nodes,
        name=name,
        llm_tag=llm_tag,
        default_agent_dir=default_agent_dir,
        default_run_dir=default_run_dir,
        params_model=params_model,
        registry=registry,
        version=version,
    )

    # Multi-command app. no_args_is_help shows the command list on bare
    # invocation; the no-op callback keeps the group structure (so a single
    # command is never collapsed to implicit — subcommands are always explicit
    # and adding commands never changes how existing ones are invoked).
    app = typer.Typer(add_completion=False, no_args_is_help=True, help=f"{name} pipeline CLI.")

    @app.callback(help=f"{name} — deterministic agent-flow pipeline. Use a subcommand (e.g. `run`).")
    def _main() -> None:
        pass

    run_cmd.register(app, ctx)
    flow_cmd.register(app, ctx)
    version_cmd.register(app, ctx)

    app()
