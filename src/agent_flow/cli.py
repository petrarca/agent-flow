"""CLI rendering helpers — optional, human-facing output on top of the logs.

The library core stays render-agnostic: it emits `Event`s via the `on_event`
callback and returns status dicts. This module turns those into nice terminal
output with `rich`. It is OPTIONAL — install the `cli` extra
(`agent-flow[cli]`) to use it. The core never imports it.

Responsibilities:
  - `event_printer(...)` -> an `on_event` callback that prints each live runner
    event (tool calls, messages, steps) as it streams.
  - `print_results_table(...)` -> a end-of-run status table (node -> outcome).
  - `run_cli(build_nodes)` -> a reusable Typer command providing the generic run
    flags (--runtime/--run-dir/--agent-dir/--instructions/--show-events/
    --llm-concurrency), a --config YAML file, and repeatable -p/--param KEY=VALUE
    for arbitrary DOMAIN params. Precedence: CLI flag > config file > default.
    A pipeline author supplies only a build_nodes() callable; no bespoke CLI.

rich/typer/yaml are imported lazily inside the functions so importing this module
(e.g. for type hints) does not hard-require the `cli` extra.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_flow.engine import Node
    from agent_flow.runners import Event


def get_console():
    """Return a shared rich Console (lazily; requires the `cli` extra)."""
    from rich.console import Console

    global _CONSOLE
    try:
        return _CONSOLE
    except NameError:
        _CONSOLE = Console()
        return _CONSOLE


def event_printer(agent: str, *, console=None) -> Callable[[Event], None]:
    """Build an `on_event` callback that prints ONE readable line per live event.

    The raw runner event (opencode NDJSON) is far too verbose to show as-is, so
    the CLI projects it to a single line using a FEW shallow, stable fields. This
    projection is a DISPLAY concern and lives here, not in the runner/engine —
    the engine never interprets event content. Unknown shapes fall back to the
    event type or a trimmed raw line, so it never breaks on a new event kind.

    Usage:
        run_agent(..., on_event=event_printer("tech-stack-analyst"))
    """
    console = console or get_console()

    def _print(ev: Event) -> None:
        line = _project_event(ev.raw)
        if line:
            console.print(f"  [dim]{agent}[/dim] {line}")

    return _print


def _project_event(raw: str) -> str:
    """Project one raw event line to a short, styled display line (best-effort)."""
    import json

    raw = raw.strip()
    if not raw.startswith("{"):
        return raw[:120]
    try:
        ev = json.loads(raw)
    except ValueError:
        return raw[:120]

    part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
    ptype = part.get("type") or ev.get("type") or "event"

    if ptype in ("step-start", "step_start"):
        return "[dim]- step[/dim]"
    if ptype in ("step-finish", "step_finish"):
        tokens = (part.get("tokens") or {}).get("total")
        return f"[green]step done[/green] [dim]({tokens:,} tokens)[/dim]" if tokens else "[green]step done[/green]"
    if ptype == "tool":
        tool = part.get("tool", "tool")
        inp = part.get("state", {}).get("input", {}) if isinstance(part.get("state"), dict) else {}
        target = inp.get("filePath") or inp.get("command") or inp.get("pattern") or ""
        return f"[cyan]tool {tool}[/cyan] {target}".rstrip()
    if ptype == "text":
        text = " ".join((part.get("text") or "").split())
        return f"[white]{text[:100]}[/white]" if text else ""
    return f"[dim]{ptype}[/dim]"


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


def print_results_table(results, *, title: str = "Pipeline results", agents: dict[str, str] | None = None, console=None) -> None:
    """Print an end-of-run node -> outcome table (agent label + per-node duration).

    `results` maps node name -> NodeOutcome (status + duration_s). A bare status
    string is also accepted (duration blank) so older callers still work.
    `agents` optionally maps node name -> agent label for an informal Agent
    column; omitted -> no Agent column.
    """
    from rich.table import Table

    console = console or get_console()
    agents = agents or {}
    show_agent = any(agents.values())
    table = Table(title=title, title_style="bold")
    table.add_column("Node")
    if show_agent:
        table.add_column("Agent", style="dim")
    table.add_column("Outcome")
    table.add_column("Duration", justify="right")
    # Outcomes are "ok" or "degraded" — a blocking failure raises NodeBlocked
    # rather than appearing here, so there is no "blocked" status to color.
    for name, outcome in results.items():
        status = getattr(outcome, "status", outcome)
        duration = getattr(outcome, "duration_s", None)
        style = {"ok": "green", "degraded": "yellow"}.get(status, "white")
        dur = f"{duration:.1f}s" if isinstance(duration, (int, float)) else ""
        row = [name] + ([agents.get(name, "")] if show_agent else []) + [f"[{style}]{status}[/{style}]", dur]
        table.add_row(*row)
    console.print(table)


def print_preflight_results(results, *, title: str = "Pre-flight checks", console=None) -> None:
    """Print pre-flight `Check` results as simple status lines (house style).

    Purely generic: it iterates whatever checks it is given and derives the
    status marker/style from each Check's `ok`/`fatal` flags — no check names,
    counts, or per-runtime knowledge are baked in. New checks (or a future
    runtime's own checks) render automatically.

    One line per check, aligned, with a leading marker:
      ok=True                -> "check" (green)  — no detail (nothing to explain)
      ok=False, fatal=True   -> "fail"  (red)    + detail — a reason not to start
      ok=False, fatal=False  -> "warn"  (yellow) + detail — non-blocking
    """
    console = console or get_console()
    console.print(f"[bold]{title}[/bold]")
    width = max((len(c.name) for c in results), default=0)
    for c in results:
        if c.ok:
            style, label, detail = "green", "check", ""
        elif c.fatal:
            style, label, detail = "red", "fail", f" — {c.detail}"
        else:
            style, label, detail = "yellow", "warn", f" — {c.detail}"
        console.print(f"  [{style}]{label:>5}[/{style}] {c.name:<{width}}{detail}")


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
    runs runtime pre-flight checks (opencode installed, not nested, agent_dir,
    prefect) — all before any token is spent.

    IMPORTANT: the calling module must have already run env.load_env() and
    _prefect_env.bootstrap() before importing Prefect (as the examples do).
    """
    import typer

    from agent_flow.run_config import build_run_config, parse_params

    app = typer.Typer(add_completion=False, help=f"Run the {name} pipeline.")

    @app.command()
    def run(
        config: str = typer.Option("", "--config", "-c", help="YAML run config (generic settings)"),
        param: list[str] | None = typer.Option(None, "--param", "-p", help="domain param KEY=VALUE (repeatable)"),  # noqa: B008 - Typer idiom
        runtime: str | None = typer.Option(None, help="opencode | mock"),
        run_dir: str | None = typer.Option(None, "--run-dir"),
        agent_dir: str | None = typer.Option(None, "--agent-dir", help="where agent definitions live (opencode --dir)"),
        instructions: str | None = typer.Option(None, "--instructions", "-i", help="run-wide brief for every agent"),
        instructions_file: str | None = typer.Option(None, "--instructions-file"),
        llm_concurrency: int | None = typer.Option(None, "--llm-concurrency"),
        show_events: bool = typer.Option(False, "--show-events", "-v", help="raw per-event firehose (instead of the live table)"),
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
            run_dir=run_dir or (default_run_dir or None),
            agent_dir=agent_dir or (default_agent_dir or None),
            instructions=instructions,
            instructions_file=instructions_file,
            llm_concurrency=llm_concurrency,
            show_events=True if show_events else None,
            model=model,
            idle_timeout_s=idle_timeout,
        )
        # 2) Domain params (validated against params_model, or untyped passthrough).
        params = _resolve_params(params_model, parse_params(param), console)
        # model / idle_timeout_s are run-wide knobs the batteries node reads from
        # params; inject the resolved values so a node uses them (a per-node
        # model=/idle_timeout_s= and an explicit -p still win, so only set if the
        # user didn't already pass them via -p).
        if cfg.model:
            params.setdefault("model", cfg.model)
        if cfg.idle_timeout_s is not None:
            params.setdefault("idle_timeout_s", str(cfg.idle_timeout_s))
        # 2b) Show the resolved settings + params (traceability before any work).
        _print_run_summary(name, cfg, params, console)
        # 3) Runtime pre-flight — abort (exit 2) on any fatal failure.
        _run_preflight(cfg.runtime, cfg.agent_dir, console)
        # 4) Run, with the chosen view (live table by default, or raw firehose).
        _run_with_view(build_nodes(), params, cfg, console, name=name, llm_tag=llm_tag)

    app()


def _print_run_summary(name: str, cfg, params: dict, console) -> None:
    """Print the resolved run settings + domain params before the run starts.

    Gives traceability: you see exactly what runtime/agent_dir/run_dir and each
    domain param resolved to (from CLI/env/.env/defaults) before any agent runs.
    run_dir is shown resolved against params (it templates at run time), so the
    actual target path is visible here too.
    """
    from agent_flow.utils import resolve_template

    try:
        shown_run_dir = resolve_template(cfg.run_dir, params, strict=True) if cfg.run_dir else "(temp)"
    except KeyError:
        shown_run_dir = cfg.run_dir  # unresolved template; the run will error clearly
    console.print(f"[bold]{name}[/bold] — resolved run")
    console.print(f"  [dim]runtime  [/dim] {cfg.runtime}")
    console.print(f"  [dim]agent_dir[/dim] {cfg.agent_dir or '(none)'}")
    console.print(f"  [dim]run_dir  [/dim] {shown_run_dir}")
    if params:
        console.print("  [dim]params[/dim]")
        for k in sorted(params):
            console.print(f"    [dim]{k}[/dim] = {params[k]}")


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


def _run_preflight(runtime: str, agent_dir: str, console) -> None:
    """Run runtime pre-flight checks; on any fatal failure show them and exit 2.

    The full check list is shown only when something fails (so a clean run stays
    quiet); the caller sees exactly which pre-conditions are missing.
    """
    from agent_flow import preflight

    results = preflight.check(runtime, agent_dir)
    failures = preflight.fatal_failures(results)
    if failures:
        print_preflight_results(results, title="Pre-flight checks (run aborted)", console=console)
        missing = ", ".join(c.name for c in failures)
        console.print(f"[red]Cannot start:[/red] {len(failures)} pre-condition(s) not met: {missing}")
        sys.exit(2)


def _run_with_view(nodes, params, cfg, console, *, name: str, llm_tag: str) -> None:
    """Run the pipeline under the chosen view, then print the results table.

    Default: line-based node progress (NodeProgressPrinter) — simple prints, no
    TUI. With --show-events: the raw per-event firehose instead. Either way the
    end-of-run results table is printed.

    Ctrl-C is handled cleanly: run_agent kills the agent's process group and we
    exit 130 (SIGINT) with a short message instead of a raw traceback.
    """

    def _raw_event_factory(agent):
        return event_printer(agent, console=console)

    if cfg.show_events:
        on_event_factory = _raw_event_factory
        on_node_event = None
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
        )
    except KeyboardInterrupt:
        console.print("[yellow]Interrupted[/yellow] — stopped by user (Ctrl-C).")
        sys.exit(130)


def _build_and_run(nodes, params, cfg, console, *, name, llm_tag, on_event_factory, on_node_event, render_results):
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
    )
    result = pipeline(run_dir=cfg.run_dir, runtime=cfg.runtime, **params)
    if render_results:
        agents = {n.name: n.agent for n in nodes}
        print_results_table(result, title=name, agents=agents, console=console)
    return result
