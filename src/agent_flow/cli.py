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


def print_results_table(results: dict[str, str], *, title: str = "Pipeline results", console=None) -> None:
    """Print an end-of-run stage -> outcome table."""
    from rich.table import Table

    console = console or get_console()
    table = Table(title=title, title_style="bold")
    table.add_column("Stage")
    table.add_column("Outcome")
    # Outcomes are "ok" or "degraded" — a blocking failure raises NodeBlocked
    # rather than appearing here, so there is no "blocked" status to color.
    for name, outcome in results.items():
        style = {"ok": "green", "degraded": "yellow"}.get(outcome, "white")
        table.add_row(name, f"[{style}]{outcome}[/{style}]")
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
    AGENT_FLOW_* > .env > --config YAML > default).

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
    import sys

    import typer

    from agent_flow import preflight
    from agent_flow.engine import build_flow
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
        show_events: bool = typer.Option(False, "--show-events", "-v", help="stream live agent events"),
    ) -> None:
        console = get_console()

        # 1) Generic run settings (RunConfig). --agent-dir default falls back to
        #    the pipeline's own definitions dir when nothing else sets it.
        cfg = build_run_config(
            config_file=config or None,
            runtime=runtime,
            run_dir=run_dir,
            agent_dir=agent_dir or (default_agent_dir or None),
            instructions=instructions,
            instructions_file=instructions_file,
            llm_concurrency=llm_concurrency,
            show_events=True if show_events else None,
        )

        # 2) Domain params. With a model: validate (fail-fast exit 2) and dump to
        #    a plain str dict. Without: pass -p through untyped, as before.
        params = _resolve_params(params_model, parse_params(param), console)

        # 3) Runtime pre-flight — abort (exit 2) on any fatal failure.
        _run_preflight(cfg.runtime, cfg.agent_dir, console)

        on_event_factory = (lambda agent: event_printer(agent, console=console)) if cfg.show_events else None
        pipeline = build_flow(
            build_nodes(),
            name=name,
            llm_tag=llm_tag,
            llm_concurrency=cfg.llm_concurrency,
            on_event_factory=on_event_factory,
            shared_instructions=cfg.resolved_instructions(),
            agent_dir=cfg.agent_dir,
        )
        result = pipeline(run_dir=cfg.run_dir, runtime=cfg.runtime, **params)
        print_results_table(result, title=name, console=console)

    def _resolve_params(model: type | None, cli_params: dict[str, str], console) -> dict:
        """Validate domain params against `model` (if any) and return a str dict.

        model is None -> pass the -p params through unchanged (untyped, as before).
        model given   -> build it with -p as init kwargs (highest) over bare env /
                         .env / defaults; on ValidationError print + exit 2. The
                         validated model is dumped in JSON mode so values are
                         plain strings/numbers ready for {name} templating.
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

        The full check table is shown only when something fails (so a clean run
        stays quiet); the caller sees exactly which pre-conditions are missing.
        """
        results = preflight.check(runtime, agent_dir)
        failures = preflight.fatal_failures(results)
        if failures:
            print_preflight_results(results, title="Pre-flight checks (run aborted)", console=console)
            missing = ", ".join(c.name for c in failures)
            console.print(f"[red]Cannot start:[/red] {len(failures)} pre-condition(s) not met: {missing}")
            sys.exit(2)

    app()
