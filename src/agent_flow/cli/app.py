"""CLI rendering helpers — human-facing output on top of the logs.

The library core stays render-agnostic: it emits `Event`s / `on_node_event`
data and returns `NodeOutcome`s. This module turns those into terminal output
with `rich`/`typer` (core dependencies; always available, never required at
core import time — see the module-level lazy imports below).

Responsibilities:
  - `event_printer(...)` -> an `on_event` callback that prints each live runner
    event (tool calls, messages, steps) as it streams, in FULL (no truncation).
  - `NodeProgressPrinter` -> the default line-based node-lifecycle view (consumes
    `on_node_event`); `print_results_table(...)` -> the end-of-run
    Node/Agent/Outcome/Duration table.
  - `print_preflight_results(...)` -> pre-flight `Check` results as status lines.
  - `run_cli(build_nodes)` -> a reusable Typer command providing the generic run
    flags (--runtime/--run-dir/--agent-dir/--instructions/--show-events/
    --llm-concurrency/--model/--idle-timeout), a --config YAML file, and
    repeatable -p/--param KEY=VALUE for DOMAIN params (optionally validated
    against a `params_model`). Precedence: CLI flag > env (AGENT_FLOW_*) > .env >
    --config YAML > default. Prints a "Resolved parameters" summary and runs
    pre-flight checks before any agent is spawned. A pipeline author supplies
    only a build_nodes() callable; no bespoke CLI.

rich/typer/yaml are imported lazily inside the functions so importing this module
(e.g. for type hints) stays cheap.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_flow.engine import Node

from agent_flow.cli.console import get_console
from agent_flow.cli.events import event_printer
from agent_flow.cli.progress import NodeProgressPrinter
from agent_flow.cli.tables import print_preflight_results, print_results_table


def run_cli(
    build_nodes: Callable[[], list[Node]],
    *,
    name: str = "agent-flow",
    llm_tag: str = "llm",
    default_agent_dir: str = "",
    default_run_dir: str = "",
    params_model: type | None = None,
) -> None:
    """Run a pipeline from a UNIFIED CLI: generic flags + --config + --param.

    A pipeline author calls this with a `build_nodes()` callable (returns the
    Node list). It provides:
      - generic run settings as flags: --runtime, --run-dir, --agent-dir,
        --instructions/--instructions-file, --llm-concurrency, --show-events;
      - --config <run.yml> for the SAME generic settings (lowest source);
      - -p/--param KEY=VALUE (repeatable) for DOMAIN params.

    Generic settings resolve via RunConfig (precedence: CLI flag > env
    AGENT_FLOW_* > .env > --config YAML > default). `default_agent_dir` /
    `default_run_dir` supply the pipeline's own fallbacks when neither the CLI nor
    env set them; `default_run_dir` may use `{param}` templating (e.g.
    "{repos_root}/{product_key}/…"), resolved strictly at run time.

    Domain params (`params_model`):
      - None (default): -p values pass straight through as an untyped string dict
        — the historical behavior, ideal for throwaway/demo flows.
      - a pydantic-settings BaseSettings subclass: -p values become init kwargs
        (highest), then bare-named env / .env, then model defaults. The model is
        VALIDATED (required fields, DirectoryPath, Literal, validators); a failure
        aborts with exit code 2 BEFORE any agent is spawned. The validated values
        are dumped back to the `params` dict the pipeline consumes — so
        downstream ({name} templating) is unchanged either way.

    Before building the flow, run_cli validates settings/params (fail fast) and
    runs runtime/backend pre-flight checks (opencode installed, not nested,
    agent_dir; prefect only when --backend prefect) — before any token is spent.

    Backend lifecycle (bootstrap/teardown) is owned by the selected backend and
    runs inside build_flow's pipeline; the caller does not bootstrap Prefect.
    The default backend is local (no Prefect); pass --backend prefect to opt in.

    Requires the `cli` extra (typer + rich): pip install 'agent-flow[cli]'.
    """
    from agent_flow.utils import require_extra

    typer = require_extra("typer", "cli", "the run_cli command")

    from agent_flow.run_config import build_run_config, parse_params, runtime_param_fields

    app = typer.Typer(add_completion=False, help=f"Run the {name} pipeline.")

    @app.command()
    def run(
        config: str = typer.Option("", "--config", "-c", help="YAML run config (generic settings)"),
        param: list[str] | None = typer.Option(None, "--param", "-p", help="domain param KEY=VALUE (repeatable)"),  # noqa: B008 - Typer idiom
        runtime: str | None = typer.Option(None, help="opencode | mock"),
        backend: str | None = typer.Option(None, "--backend", help="execution backend: local (default, no Prefect) | prefect (opt-in run UI/scale)"),
        run_dir: str | None = typer.Option(None, "--run-dir"),
        agent_dir: str | None = typer.Option(None, "--agent-dir", help="where agent definitions live (opencode --dir)"),
        instructions: str | None = typer.Option(None, "--instructions", "-i", help="run-wide brief for every agent"),
        instructions_file: str | None = typer.Option(None, "--instructions-file"),
        instruct: list[str] | None = typer.Option(  # noqa: B008 - Typer idiom
            None, "--instruct", help="per-node instruction NODE=text (repeatable); appended LAST to the node's prompt"
        ),
        start_from: str | None = typer.Option(
            None, "--start-from", help="begin at this node or parallel-group, skipping upstream (assumes their outputs already exist)"
        ),
        only: str | None = typer.Option(
            None,
            "--only",
            help="run ONLY this node/parallel-group and stop (skips everything else; assumes other outputs exist). Excludes --start-from",
        ),
        llm_concurrency: int | None = typer.Option(None, "--llm-concurrency"),
        show_events: bool = typer.Option(False, "--show-events", "-v", help="raw per-event firehose (instead of the live table)"),
        show_diffs: bool = typer.Option(
            False, "--show-diffs", help="render edit/write diffs as blocks (composes with --show-events and the live table)"
        ),
        diff_style: str | None = typer.Option(
            None, "--diff-style", help="diff layout with --show-diffs: unified (one column) | split (side-by-side)"
        ),
        model: str | None = typer.Option(None, "--model", "-m", help="model for every node (provider/model); per-node model= still overrides"),
        idle_timeout: int | None = typer.Option(
            None, "--idle-timeout", help="liveness timeout (s): kill an agent only after this long with no event/sidecar"
        ),
    ) -> None:
        console = get_console()
        # 1) Generic run settings (RunConfig). --agent-dir default falls back to
        #    the pipeline's own definitions dir when nothing else sets it.
        cfg = build_run_config(
            config_file=config or None,
            runtime=runtime,
            backend=backend,
            run_dir=run_dir or (default_run_dir or None),
            agent_dir=agent_dir or (default_agent_dir or None),
            instructions=instructions,
            instructions_file=instructions_file,
            llm_concurrency=llm_concurrency,
            show_events=True if show_events else None,
            show_diffs=True if show_diffs else None,
            diff_style=diff_style,
            model=model,
            idle_timeout_s=idle_timeout,
        )
        # Per-node instructions: CLI --instruct NODE=text merges OVER the config
        # node_instructions: (CLI wins per node). Same NODE=text shape as -p. Store
        # the merged map back on cfg so _build_and_run threads it into build_flow.
        cfg.node_instructions = {**cfg.node_instructions, **parse_params(instruct)}
        # 2) Domain params (validated against params_model, or untyped passthrough).
        params = _resolve_params(params_model, parse_params(param), console)
        # Fields the model marks as runtime-populated (json_schema_extra
        # {"runtime": True}) are NOT user inputs — they get an initial placeholder
        # and are overwritten at run time (e.g. by a node's exports). Hide them
        # from the resolved-params summary so they don't read as things you pass.
        runtime_fields = runtime_param_fields(params_model)
        # model / idle_timeout_s are run-wide knobs the batteries node reads from
        # params; inject the resolved values so a node uses them (a per-node
        # model=/idle_timeout_s= and an explicit -p still win, so only set if the
        # user didn't already pass them via -p).
        # idle_timeout_s is always concrete (RunConfig defaults it); inject it.
        # model is injected ONLY if set — empty means "let the runtime decide"
        # (the runner omits --model), so we never force a model into params.
        # A per-node agent_node(...) and an explicit -p still override.
        if cfg.model:
            params.setdefault("model", cfg.model)
        params.setdefault("idle_timeout_s", str(cfg.idle_timeout_s))
        # 2b) Show the resolved settings + params (traceability before any work).
        _print_run_summary(name, cfg, params, console, hide=runtime_fields)
        # 3) Runtime pre-flight — abort (exit 2) on any fatal failure.
        _run_preflight(cfg.runtime, cfg.agent_dir, cfg.backend, console)
        # 4) Run, with the chosen view (live table by default, or raw firehose).
        if start_from and only:
            console.print(
                "[red]--only and --start-from are mutually exclusive[/red] (--only runs a single group; --start-from runs from a group to the end)."
            )
            raise typer.Exit(2)
        _run_with_view(build_nodes(), params, cfg, console, name=name, llm_tag=llm_tag, start_from=start_from or "", only=only or "")

    app()


def _print_run_summary(name: str, cfg, params: dict, console, *, hide: set[str] | None = None) -> None:
    """Print the resolved run settings + domain params before the run starts.

    Gives traceability: you see exactly what runtime/backend/agent_dir/run_dir and
    each domain param resolved to (from CLI/env/.env/defaults) before any agent runs.
    run_dir is shown resolved against params (it templates at run time), so the
    actual target path is visible here too. `hide` names params to omit (fields
    the model marked runtime-populated — placeholders, not user inputs).
    """
    hide = hide or set()
    from agent_flow.utils import resolve_template

    try:
        shown_run_dir = resolve_template(cfg.run_dir, params, strict=True) if cfg.run_dir else "(temp)"
    except KeyError:
        shown_run_dir = cfg.run_dir  # unresolved template; the run will error clearly
    # Uniform `key = value` lines under a clear headline. Settings first, then
    # the domain params — everything the run resolved to, before any work.
    console.print(f"[bold]Resolved parameters[/bold] [dim]({name})[/dim]")
    settings = {
        "runtime": cfg.runtime,
        "backend": cfg.backend,
        "agent_dir": cfg.agent_dir or "(none)",
        "run_dir": shown_run_dir,
        "model": cfg.model or "(runtime default)",  # empty -> opencode resolves it
        "idle_timeout_s": cfg.idle_timeout_s,
    }
    rows = {**settings, **params}
    width = max((len(k) for k in rows), default=0)
    for k, v in settings.items():
        console.print(f"  [cyan]{k:<{width}}[/cyan] = {v}")
    # Domain params, minus any already shown as a setting (model/idle_timeout_s
    # are injected into params but belong under settings) and minus runtime-
    # populated fields (placeholders, not user inputs).
    for k in sorted(k for k in params if k not in settings and k not in hide):
        console.print(f"  [cyan]{k:<{width}}[/cyan] = {params[k]}")


def _resolve_params(model: type | None, cli_params: dict[str, str], console) -> dict:
    """Validate domain params against `model` (if any) and return a str dict.

    model is None -> pass the -p params through unchanged (untyped, as before).
    model given   -> build it with -p as init kwargs (highest) over bare env /
                     .env / defaults; on ValidationError print + exit 2. The
                     validated model is dumped in JSON mode so values are plain
                     strings/numbers ready for {name} templating.
    """
    if model is None:
        return dict(cli_params)
    from pydantic import ValidationError

    try:
        settings = model(**cli_params)
    except ValidationError as exc:
        console.print(f"[red]Invalid parameters for {getattr(model, '__name__', 'params')}:[/red]")
        for err in exc.errors():
            loc = ".".join(str(p) for p in err.get("loc", ()))
            console.print(f"  [red]-[/red] {loc}: {err.get('msg')}")
        sys.exit(2)
    return {k: ("" if v is None else str(v)) for k, v in settings.model_dump(mode="json").items()}


def _run_preflight(runtime: str, agent_dir: str, backend: str, console) -> None:
    """Run runtime/backend pre-flight checks; on any fatal failure show them and exit 2.

    The full check list is shown only when something fails (so a clean run stays
    quiet); the caller sees exactly which pre-conditions are missing.
    """
    from agent_flow import preflight

    results = preflight.check(runtime, agent_dir, backend)
    failures = preflight.fatal_failures(results)
    if failures:
        print_preflight_results(results, title="Pre-flight checks (run aborted)", console=console)
        missing = ", ".join(c.name for c in failures)
        console.print(f"[red]Cannot start:[/red] {len(failures)} pre-condition(s) not met: {missing}")
        sys.exit(2)


def _run_with_view(nodes, params, cfg, console, *, name: str, llm_tag: str, start_from: str = "", only: str = "") -> None:
    """Run the pipeline under the chosen view, then print the results table.

    Two independent knobs compose (see the matrix):
      - --show-events -> per-event firehose (replaces the node-progress table).
      - --show-diffs  -> render edit/write diff blocks; layers onto EITHER the
        firehose or the default table.

      | flags                       | base view      | diff blocks |
      | (none)                      | progress table | no          |
      | --show-diffs                | progress table | yes         |
      | --show-events               | firehose       | no          |
      | --show-events --show-diffs  | firehose       | yes         |

    Either way the end-of-run results table is printed. Ctrl-C is handled cleanly:
    run_agent kills the agent's process group and we exit 130 (SIGINT).
    """

    def _event_factory(label):
        # lines only in firehose mode; diffs whenever --show-diffs is on.
        return event_printer(label, console=console, lines=cfg.show_events, diffs=cfg.show_diffs, diff_style=cfg.diff_style)

    if cfg.show_events or cfg.show_diffs:
        # An event callback is needed for the firehose and/or diff blocks. Keep
        # the node-progress table UNLESS the firehose replaces it (--show-events).
        on_event_factory = _event_factory
        on_node_event = None if cfg.show_events else NodeProgressPrinter(console=console).on_node_event
    else:
        on_event_factory = None
        on_node_event = NodeProgressPrinter(console=console).on_node_event

    try:
        _build_and_run(
            nodes,
            params,
            cfg,
            console,
            name=name,
            llm_tag=llm_tag,
            on_event_factory=on_event_factory,
            on_node_event=on_node_event,
            render_results=True,
            start_from=start_from,
            only=only,
        )
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted[/yellow] — stopped by user (Ctrl-C).")
        sys.exit(130)


def _build_and_run(nodes, params, cfg, console, *, name, llm_tag, on_event_factory, on_node_event, render_results, start_from="", only=""):
    """Compile the flow with the given hooks and run it; optionally print results."""
    from agent_flow.engine import build_flow

    pipeline = build_flow(
        nodes,
        name=name,
        llm_tag=llm_tag,
        llm_concurrency=cfg.llm_concurrency,
        on_event_factory=on_event_factory,
        on_node_event=on_node_event,
        shared_instructions=cfg.resolved_instructions(),
        agent_dir=cfg.agent_dir,
        node_instructions=cfg.node_instructions,
        backend=cfg.backend,
    )
    # start_from / only are per-INVOCATION forward-entry knobs (not persisted
    # config): start_from begins at a group and runs forward; only runs a single
    # group and stops. Mutually exclusive (validated at the CLI and in _pipeline).
    call_kwargs = {"run_dir": cfg.run_dir, "runtime": cfg.runtime, **params}
    if start_from:
        call_kwargs["start_from"] = start_from
    if only:
        call_kwargs["only"] = only
    result = pipeline(**call_kwargs)
    if render_results:
        agents = {n.name: n.agent for n in nodes}
        print_results_table(result, title=name, agents=agents, console=console)
    return result
