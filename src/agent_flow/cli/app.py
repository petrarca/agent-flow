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
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from agent_flow.engine import Node
    from agent_flow.flowdef import FlowDef
    from agent_flow.run_config import RunConfig


def run_cli(
    flow: FlowDef | Callable[[], list[Node]],
    *,
    name: str = "agent-flow",
    llm_tag: str = "llm",
    run_config: dict[str, Any] | RunConfig | None = None,
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

    `run_config` supplies the pipeline's own run-config defaults (a dict or a
    RunConfig) — e.g. `{"agent_dir": ..., "run_dir": "{param}/out"}`. Here it is
    always the LOWEST explicit source: CLI flags, env, .env and --config override
    it, because this CLI is the resolver. (Programmatically, `run_flow`/`arun_flow`
    honour a RunConfig verbatim instead — see their docstrings.) `run_dir` may use
    `{param}` templating, resolved strictly at run time.

    Domain params — the flow's SIGNATURE (what it needs to RUN, vs `run_config`
    which is how/where it runs). Two ways to declare it:
      - DECLARATIVE (preferred): `FlowDef(params_schema="CloudParams")` naming a
        model registered with `registry.params_model(...)`. The contract travels
        WITH the flow, so a serialized FlowDef is self-describing and two flows in
        one app cannot be paired with the wrong params.
      - IMPERATIVE: `params_model=` here — for the `build_nodes` callable form
        (no FlowDef to declare on), or to override a flow's declared schema.
        An explicit `params_model=` wins over `FlowDef.params_schema`.
    Either way:
      - unset: -p values pass through as an untyped string dict.
      - a pydantic model (plain `BaseModel`, or `BaseSettings` to add bare-env /
        .env fallback): -p become init kwargs (highest); VALIDATED (required
        fields, DirectoryPath, Literal, validators), a failure aborts with exit 2
        before any agent spawns. The validated values are dumped back to the
        `params` dict, so downstream {name} templating is unchanged either way.

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
        from agent_flow.registry import FlowRegistry

        if registry is None:
            registry = FlowRegistry()
        flow_def = flow
        # The public `registry` param is typed `object` (any registry-like), but a
        # FlowDef flow resolves names through the real FlowRegistry API.
        reg = cast("FlowRegistry", registry)
        build_nodes = lambda: compile_flow(flow_def, reg)  # noqa: E731
        if name == "agent-flow":
            name = flow_def.name
        # `run_context` and `run_instructions` are pipeline DECLARATIONS, not
        # per-run knobs — they must be threaded through, or a FlowDef's run-wide
        # rules/brief would be silently dropped under run_cli while working under
        # run_flow. The -i/--instructions value APPENDS to run_instructions (it
        # does not replace it). (agent_dir/backend/llm_concurrency are no longer
        # on the FlowDef — they are run config; pass them via run_config=/CLI/env.)
        run_context = tuple(flow_def.run_context)
        run_instructions = flow_def.run_instructions
        # The flow's SIGNATURE: it declares the params it needs via a registered
        # name, so the pairing "flow <-> its params" travels WITH the flow instead
        # of relying on the call site to pass the matching model. An explicit
        # params_model= still wins (the imperative escape hatch, and an override).
        if params_model is None and flow_def.params_schema:
            params_model = reg.get_params_model(flow_def.params_schema)
    else:
        build_nodes = flow
        run_context = ()
        run_instructions = ""

    from agent_flow.run_config import normalize_run_config

    ctx = RunCliContext(
        build_nodes=build_nodes,
        run_context=run_context,
        run_instructions=run_instructions,
        name=name,
        llm_tag=llm_tag,
        run_config=normalize_run_config(run_config) or {},
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
