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


def event_printer(label: str, *, console=None, lines: bool = True, diffs: bool = False, diff_style: str = "unified") -> Callable[[Event], None]:
    """Build an `on_event` callback that renders live events.

    `label` is the prefix each line carries. The engine passes the NODE name (the
    DAG unit the reader navigates by), not the agent that implements it — in a
    firehose of live lines the node is what tells you where in the flow you are.

    Two INDEPENDENT, composable outputs (from --show-events / --show-diffs):
      - `lines`  : one styled progress line per event (the firehose).
      - `diffs`  : for a file-changing event carrying a diff, a rendered diff
                   block (unified or split, per `diff_style`) AFTER its line.
                   Works with or without `lines`, so `--show-diffs` alone layers
                   diffs onto the default table view.

    Rendering is RUNNER-AGNOSTIC: the runner already normalized the event into
    neutral fields (kind/title/detail/status/diff) in `parse_event`; here we only
    map those to this CLI's palette. The CLI never re-parses the runtime's wire
    format — that knowledge lives in the runner.

    Usage:
        run_agent(..., on_event=event_printer("analyst", diffs=True))
    """
    console = console or get_console()

    def _print(ev: Event) -> None:
        if lines:
            line = render_event(ev)
            if line:
                console.print(f"  [dim]{label}[/dim] {line}")
        if diffs and ev.diff:
            render_diff(ev, console=console, style=diff_style)

    return _print


def render_diff(ev: Event, *, console=None, style: str = "unified") -> None:
    """Render a file-change event's diff. `style` is the user's choice:

      - "unified"  : one column, red removals / green additions, header stripped
                     (OpenCode's headless look; robust on any terminal width).
      - "split"    : side-by-side two columns (old | new), 50/50, for wide
                     terminals and large edits.

    Rich has no native diff widget; both are built rich-only by parsing the diff
    (shared `_diff_rows`) and laying it out. We strip HEADER noise
    (Index:/===/---/+++) since the tool line already names the file. The block is
    bracketed by a thin top rule LABELLED with the file (from `ev.title`, since we
    stripped the diff's own filename header) and a plain bottom rule, so it stands
    out from surrounding log lines. Reads only neutral `ev` fields. Never raises —
    falls back to a plain colored block.
    """
    if not ev.diff:
        return
    console = console or get_console()
    try:
        from rich.rule import Rule

        label = ev.title or "diff"
        console.print(Rule(f"[dim]{label}[/dim]", style="dim", characters="\u2500"))
        if style == "split":
            _render_side_by_side(ev.diff, console)
        else:
            _render_unified(ev.diff, console)
        console.print(Rule(style="dim", characters="\u2500"))
    except Exception:  # noqa: BLE001 - display must never break a run
        from rich.syntax import Syntax

        console.print(Syntax(ev.diff, "diff", theme="ansi_dark", background_color="default", word_wrap=True))


def _render_unified(diff: str, console) -> None:
    """One-column diff: header stripped, magenta hunks, red/green change lines."""
    from rich.text import Text

    for kind, left, right in _diff_rows(diff):
        if kind == "hdr":
            console.print(Text(left, style="magenta"))
        elif kind == "ctx":
            console.print(Text("  " + left, style="dim"))
        else:  # chg — emit removal then addition, sign-colored
            if left:
                console.print(Text("- " + left, style="red"))
            if right:
                console.print(Text("+ " + right, style="green"))


def _diff_rows(diff: str) -> list[tuple[str, str, str]]:
    """Parse a unified diff into aligned (kind, old, new) rows for a 2-col view.

    kind is "ctx" | "chg" (change) | "hdr" (hunk @@ header). Header noise
    (Index:/===/--- /+++ ) is dropped. Consecutive '-'/'+' lines are paired
    positionally (removal left, addition right); unbalanced ones pad with "".
    """
    rows: list[tuple[str, str, str]] = []
    removed: list[str] = []
    added: list[str] = []

    def flush() -> None:
        for i in range(max(len(removed), len(added))):
            rows.append(("chg", removed[i] if i < len(removed) else "", added[i] if i < len(added) else ""))
        removed.clear()
        added.clear()

    for line in diff.splitlines():
        if line.startswith(("Index:", "===", "--- ", "+++ ", "diff --git")):
            continue  # header noise — the tool line already names the file
        if line.startswith("@@"):
            flush()
            rows.append(("hdr", line, ""))
        elif line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
        else:
            flush()
            rows.append(("ctx", line[1:] if line.startswith(" ") else line, ""))
    flush()
    return rows


def _render_side_by_side(diff: str, console) -> None:
    """Render parsed diff rows as a borderless two-column rich Table."""
    from rich.table import Table
    from rich.text import Text

    rows = _diff_rows(diff)
    if not rows:
        return
    table = Table(show_header=False, show_edge=False, box=None, padding=(0, 1), expand=True)
    table.add_column(ratio=1, overflow="fold")
    table.add_column(ratio=1, overflow="fold")
    for kind, left, right in rows:
        if kind == "hdr":
            table.add_row(Text(left, style="magenta"), Text(""))
        elif kind == "ctx":
            table.add_row(Text(left, style="dim"), Text(left, style="dim"))
        else:  # chg — color alone carries add/remove (no -/+ glyph clutter)
            lt = Text(left, style="red") if left else Text("")
            rt = Text(right, style="green") if right else Text("")
            table.add_row(lt, rt)
    console.print(table)


def render_event(ev: Event) -> str:
    """Style one NEUTRAL event into a rich display line (this CLI's palette).

    The runner owns classification (ev.kind/status) and content (ev.title/detail);
    this owns COLOR. `kind` drives the base style; for tools, `status` refines it
    (running=cyan, completed=green, error=red) — a richer coloring than a flat
    hue, made possible by the neutral status field.

    We deliberately color only the LEADING keyword ("tool"/"step done") and leave
    the CONTENT (ev.title — file paths, patterns, URLs) UNSTYLED. rich's default
    highlighter then auto-colors those tokens (paths magenta, numbers, quoted
    strings, …). Wrapping the whole line in an explicit style would suppress that,
    so the keyword carries our semantic status color and rich decorates the rest.

    Content is shown in FULL (rich soft-wraps); --show-events is a debug stream
    where seeing the whole line matters more than fitting one physical row.

    Never raises: an unknown kind falls back to the (already trimmed) title.
    """
    kind = ev.kind
    if kind == "step_start":
        return "[dim]- step[/dim]"
    if kind == "step_end":
        return f"[green]step done[/green] [dim]({ev.tokens:,} tokens)[/dim]" if ev.tokens else "[green]step done[/green]"
    if kind == "tool":
        style = {"error": "red", "completed": "green"}.get(ev.status, "cyan")
        # Prefer a diff stat (+A/-D) built here from the neutral counts; else the
        # runner's non-diff hint (matches / exit code). Formatting lives in the
        # CLI — the runner only supplies numbers.
        hint = f"+{ev.added}/-{ev.removed}" if (ev.added or ev.removed) else ev.detail
        detail = f" [dim]({hint})[/dim]" if hint else ""
        # Color the keyword by status; leave the title bare for rich to highlight.
        return f"[{style}]tool[/{style}] {ev.title}{detail}".rstrip()
    if kind == "text":
        return f"[white]{ev.title}[/white]" if ev.title else ""
    # "other" / unknown: show the title (the event type) dimmed, if any.
    return f"[dim]{ev.title}[/dim]" if ev.title else ""


def _project_event(raw: str) -> str:
    """Back-compat shim: render a raw opencode line via the neutral path.

    The opencode wire-shape knowledge now lives in OpenCodeRunner.parse_event;
    this only re-parses a raw line through it and styles the result, so callers
    (and tests) that still pass a raw string keep working. The LIVE path uses the
    runner-filled Event directly and never touches this.
    """
    from agent_flow.runners import OpenCodeRunner

    raw = raw.strip()
    if not raw.startswith("{"):
        return raw
    ev = OpenCodeRunner().parse_event(raw)
    return render_event(ev) if ev.is_event else raw


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

    from agent_flow.run_config import build_run_config, parse_params, runtime_param_fields

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
        _run_preflight(cfg.runtime, cfg.agent_dir, console)
        # 4) Run, with the chosen view (live table by default, or raw firehose).
        _run_with_view(build_nodes(), params, cfg, console, name=name, llm_tag=llm_tag)

    app()


def _print_run_summary(name: str, cfg, params: dict, console, *, hide: set[str] | None = None) -> None:
    """Print the resolved run settings + domain params before the run starts.

    Gives traceability: you see exactly what runtime/agent_dir/run_dir and each
    domain param resolved to (from CLI/env/.env/defaults) before any agent runs.
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
