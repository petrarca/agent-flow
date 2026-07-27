"""The `run` command — execute the pipeline.

Registered onto the shared Typer app by run_cli via `register(app, ctx)`. Owns
all the generic run flags (--runtime/--backend/--start-from/--only/…), resolves
RunConfig + domain params, runs pre-flight, and executes the flow under the
chosen view.
"""

from __future__ import annotations

import sys

from agent_flow.cli.console import get_console
from agent_flow.cli.context import RunCliContext
from agent_flow.cli.events import event_printer
from agent_flow.cli.progress import NodeProgressPrinter
from agent_flow.cli.tables import print_preflight_results, print_results_table


def register(app, ctx: RunCliContext) -> None:
    """Attach the `run` command to `app`."""
    import typer

    from agent_flow.run_config import build_run_config, parse_params, runtime_param_fields

    @app.command()
    def run(
        config: str = typer.Option("", "--config", "-c", help="YAML run config (generic settings)"),
        param: list[str] | None = typer.Option(None, "--param", "-p", help="domain param KEY=VALUE (repeatable)"),  # noqa: B008 - Typer idiom
        runtime: str | None = typer.Option(None, help="real out-of-process runner, e.g. 'opencode'"),
        mock_agents: bool | None = typer.Option(
            None,
            "--mock-agents/--no-mock-agents",
            help="mock mode: run nodes whose agent has a registered mock_agent deterministically (no tokens); others still run for real. "
            "--no-mock-agents forces it off (overriding AGENT_FLOW_MOCK_AGENTS).",
        ),
        backend: str | None = typer.Option(
            None, "--backend", help="execution backend: inprocess (default, no Prefect) | prefect (opt-in run UI/scale)"
        ),
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
        log_level: str = typer.Option(
            "INFO", "--log-level", help="library log level: DEBUG | INFO | WARNING | ERROR (engine/supervision lines via loguru to stderr)"
        ),
    ) -> None:
        """Run the pipeline."""
        from agent_flow.logging_setup import setup_logging

        # Configure loguru (+ stdlib intercept) FIRST, so preflight and the whole
        # run emit visible, level-controlled logs to stderr. DEBUG surfaces node
        # start/finish, run_dir, jump-backs, and per-node executor selection.
        setup_logging(log_level)
        console = get_console()
        cfg = build_run_config(
            config_file=config or None,
            runtime=runtime,
            mock_agents=mock_agents,  # None (env/config wins) | True | False (explicit)
            backend=backend,
            run_dir=run_dir or (ctx.default_run_dir or None),
            agent_dir=agent_dir or (ctx.default_agent_dir or None),
            instructions=instructions,
            instructions_file=instructions_file,
            llm_concurrency=llm_concurrency,
            show_events=True if show_events else None,
            show_diffs=True if show_diffs else None,
            diff_style=diff_style,
            model=model,
            idle_timeout_s=idle_timeout,
        )
        # Per-node instructions: CLI --instruct NODE=text populates
        # nodes.<node>.instructions, winning over a config `nodes:` entry per node.
        for node_name, text in parse_params(instruct).items():
            cfg.set_node_instruction(node_name, text)
        params = _resolve_params(ctx.params_model, parse_params(param), console)
        runtime_fields = runtime_param_fields(ctx.params_model)
        # model / idle_timeout_s are run-wide knobs an agent-node reads from
        # params; inject the resolved values (an explicit -p / per-node value still
        # wins). model only when set (empty -> the runtime resolves it).
        if cfg.model:
            params.setdefault("model", cfg.model)
        params.setdefault("idle_timeout_s", str(cfg.idle_timeout_s))
        _print_run_summary(ctx.name, cfg, params, console, hide=runtime_fields)
        # Preflight validates the real runtime + agent-dir. Under full mock mode a
        # flow may have no agent-dir; skip the runtime preflight then (partial
        # mocking still needs the runner for un-mocked nodes, but we cannot tell
        # here which nodes are mocked, so mock mode relaxes the check — see #11).
        if not cfg.mock_agents:
            _run_preflight(cfg.runtime, cfg.agent_dir, cfg.backend, console)
        if start_from and only:
            console.print(
                "[red]--only and --start-from are mutually exclusive[/red] (--only runs a single group; --start-from runs from a group to the end)."
            )
            raise typer.Exit(2)
        _run_with_view(
            ctx.build_nodes(),
            params,
            cfg,
            console,
            name=ctx.name,
            llm_tag=ctx.llm_tag,
            start_from=start_from or "",
            only=only or "",
            registry=ctx.registry,
            run_context=ctx.run_context,
        )


def _print_run_summary(name: str, cfg, params: dict, console, *, hide: set[str] | None = None) -> None:
    """Print the resolved run settings + domain params before the run starts.

    Gives traceability: you see exactly what runtime/backend/agent_dir/run_dir and
    each domain param resolved to (from CLI/env/.env/defaults) before any agent runs.
    run_dir is shown resolved against params (it templates at run time). `hide`
    names params to omit (fields the model marked runtime-populated).
    """
    hide = hide or set()
    from agent_flow.utils import resolve_template

    try:
        shown_run_dir = resolve_template(cfg.run_dir, params, strict=True) if cfg.run_dir else "(temp)"
    except KeyError:
        shown_run_dir = cfg.run_dir  # unresolved template; the run will error clearly
    console.print(f"[bold]Resolved parameters[/bold] [dim]({name})[/dim]")
    settings = {
        "runtime": cfg.runtime,
        "mock_agents": "on" if cfg.mock_agents else "off",
        "backend": cfg.backend,
        "agent_dir": cfg.agent_dir or "(none)",
        "run_dir": shown_run_dir,
        "model": cfg.model or "(runtime default)",  # empty -> the runtime resolves it
        "idle_timeout_s": cfg.idle_timeout_s,
    }
    per_node = getattr(cfg, "nodes", None)
    if per_node:
        # Name the nodes carrying a per-node override, so a `nodes:` entry is
        # visible in the pre-run summary rather than only taking effect silently.
        settings["per-node config"] = ", ".join(sorted(per_node))
    rows = {**settings, **params}
    width = max((len(k) for k in rows), default=0)
    for k, v in settings.items():
        console.print(f"  [cyan]{k:<{width}}[/cyan] = {v}")
    for k in sorted(k for k in params if k not in settings and k not in hide):
        console.print(f"  [cyan]{k:<{width}}[/cyan] = {params[k]}")


def _resolve_params(model: type | None, cli_params: dict[str, str], console) -> dict:
    """Validate domain params against `model` (if any) and return a str dict.

    model None -> pass -p through unchanged (untyped). model given -> build it
    with -p as init kwargs over bare env/.env/defaults; on ValidationError print +
    exit 2. The validated model is dumped in JSON mode for {name} templating.
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
    """Run runtime/backend pre-flight checks; on any fatal failure show them and exit 2."""
    from agent_flow import preflight

    results = preflight.check(runtime, agent_dir, backend)
    failures = preflight.fatal_failures(results)
    if failures:
        print_preflight_results(results, title="Pre-flight checks (run aborted)", console=console)
        missing = ", ".join(c.name for c in failures)
        console.print(f"[red]Cannot start:[/red] {len(failures)} pre-condition(s) not met: {missing}")
        sys.exit(2)


def _run_with_view(
    nodes, params, cfg, console, *, name: str, llm_tag: str, start_from: str = "", only: str = "", registry=None, run_context: tuple[str, ...] = ()
) -> None:
    """Run the pipeline under the chosen view, then print the results table.

      | flags                       | base view      | diff blocks |
      | (none)                      | progress table | no          |
      | --show-diffs                | progress table | yes         |
      | --show-events               | firehose       | no          |
      | --show-events --show-diffs  | firehose       | yes         |

    Ctrl-C is handled cleanly: run_agent kills the agent's process group and we
    exit 130 (SIGINT).
    """

    def _event_factory(label):
        return event_printer(label, console=console, lines=cfg.show_events, diffs=cfg.show_diffs, diff_style=cfg.diff_style)

    if cfg.show_events or cfg.show_diffs:
        on_event_factory = _event_factory
        on_node_event = None if cfg.show_events else NodeProgressPrinter(console=console).on_node_event
    else:
        on_event_factory = None
        on_node_event = NodeProgressPrinter(console=console).on_node_event

    from agent_flow.engine import NodeBlocked

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
            registry=registry,
            run_context=run_context,
        )
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted[/yellow] — stopped by user (Ctrl-C).")
        sys.exit(130)
    except NodeBlocked as exc:
        # A blocking node failed — an EXPECTED terminal outcome, not a crash.
        # Print a clean error and exit 1; no Python traceback (the cause is in
        # the message, e.g. the runtime error + the exact command).
        console.print(f"[red]Run failed[/red] — {exc}")
        sys.exit(1)


def _build_and_run(
    nodes,
    params,
    cfg,
    console,
    *,
    name,
    llm_tag,
    on_event_factory,
    on_node_event,
    render_results,
    start_from="",
    only="",
    registry=None,
    run_context: tuple[str, ...] = (),
):
    """Compile the flow with the given hooks and run it; optionally print results."""
    import anyio

    from agent_flow.engine import build_flow

    pipeline = build_flow(
        nodes,
        name=name,
        llm_tag=llm_tag,
        llm_concurrency=cfg.llm_concurrency,
        on_event_factory=on_event_factory,
        on_node_event=on_node_event,
        run_instructions=cfg.resolved_instructions(),
        run_context=run_context,
        agent_dir=cfg.agent_dir,
        node_overrides={n: nc.model_dump() for n, nc in cfg.nodes.items()},
        durations=cfg.durations,
        options=cfg.options,
        backend=cfg.backend,
        registry=registry,
    )
    # start_from / only are per-INVOCATION forward-entry knobs (not persisted).
    # Framework knobs travel alongside runtime/run_dir, not in domain params.
    call_kwargs = {"run_dir": cfg.run_dir, "runtime": cfg.runtime, "mock_agents": cfg.mock_agents, **params}
    if start_from:
        call_kwargs["start_from"] = start_from
    if only:
        call_kwargs["only"] = only
    # The CLI stays sync; the engine pipeline is async. Single anyio.run bridge.
    result = anyio.run(lambda: pipeline(**call_kwargs))
    if render_results:
        agents = {n.name: n.agent for n in nodes}
        print_results_table(result, title=name, agents=agents, console=console)
