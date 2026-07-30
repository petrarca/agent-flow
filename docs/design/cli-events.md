---
type: Concept
title: CLI and live events
description: The Event stream and on_event hook, the runner-agnostic neutral display view the CLI renders, and the optional Typer/rich CLI.
tags: [agent-flow, cli, events, on_event, rich, typer, observability]
timestamp: 2026-07-23T07:51:35Z
---

# CLI and live events

Three complementary observability channels, layered:

- **Logs (backend logger, INFO)** — the always-on diagnostics: node start/finish,
  jump-backs, concurrency setup, errors. The default in-process backend uses stdlib
  logging; the opt-in Prefect backend uses Prefect's logger. Default level; for
  debugging and the essential record.
- **Node progress (default)** — a line per node transition (running →
  ok/degraded/failed, with the agent label and elapsed time) plus an end-of-run
  results table. Driven by the node-lifecycle hook (below), rendered by the CLI.
- **Events (`--show-events`)** — the raw per-event firehose: a one-line
  projection of every agent event, for debugging. Replaces the progress lines
  when enabled (mutually exclusive views).

## Node-lifecycle hook (the coarse channel)

`build_flow(on_node_event=<cb>)` takes an optional
`(node_name, phase, status, agent) -> None` callback the engine calls at each
node's start (`phase="start"`, `status=None`) and finish
(`phase="finish"`, `status` = `"ok"`/`"degraded"`, or `"failed"` on a blocking
error) — including on re-runs (a jumped-back node fires `start` again). It is
**pure data**: the engine never renders. `agent` is `Node.agent`, an informal
label (set by `agent_node`; `""` for hand-written nodes) so a view can show which
agent a node runs.

Per-node duration is timed where the node runs (`NodeOutcome.duration_s`) and
carried through the flow result (`dict[str, NodeOutcome]`), so the results table
shows Node | Agent | Outcome | Duration without the CLI reconstructing timing.
The Agent cell is runtime-qualified `<runtime>:<agent>` (e.g.
`opencode:tech-stack-analyst`, `inproc:classify`, `mock:verifier`): each executor
stamps its runtime onto `AgentResult.runtime`, which flows to `NodeOutcome.runtime`
and is joined with the agent name via the canonical `qualified_agent()` helper.
This makes partial mocking and in-process nodes visible at a glance.

The default CLI view (`NodeProgressPrinter`) is deliberately line-based, not a
repainting `rich.Live`/TUI: a Live table fights the backends' threaded task
execution and interleaved logging (duplicated frames, corrupted output). A plain
`console.print` per transition interleaves cleanly with logs and is robust in
non-TTY/CI. A consumer who wants a richer TUI can build one on the same hooks
(`on_node_event` + `on_event_factory`) — the library ships only the simple
default.

## Interrupt (Ctrl-C)

`_supervise` catches `KeyboardInterrupt` and kills the agent's process group
(SIGTERM→SIGKILL) before re-raising, so Ctrl-C never orphans an opencode process
(or its MCP children). The `run` command (`cli/commands/run.py`) catches it,
prints a short `Interrupted` line, and exits 130 (standard SIGINT code) — not a
raw traceback.

## The event hook (render-agnostic core)

Each runner stdout line the [supervisor](supervision.md) sees becomes an `Event`
carrying the supervision fields (`tokens`, `cost`, `is_terminal`, `is_event`) and
the raw original line. `run_agent` accepts an `on_event: (Event) -> None`
callback, invoked per event; supervision ignores it, and display errors are
swallowed so they can never disrupt a run.

The engine does not interpret event content or render anything — it stays
render-agnostic, using only the supervision fields (`tokens`/`cost`/`is_terminal`).
The *display* fields are filled by the runner (the only thing that understands
its wire format), so the coupling to a runtime's (versioned) event schema lives in
exactly one place.

## The neutral display view (runner fills, CLI renders)

The one place a runtime's wire shape is interpreted is the runner's `parse_event`.
Besides supervision fields, it normalizes each event into a runner-agnostic
display view on `Event`:

```
kind          "step_start" | "step_end" | "tool" | "text" | "other"
title         primary human summary (tool title/target, the message text, …)
detail        secondary NON-DIFF hint (tool metadata: "12 matches", "exit 0")
status        tool lifecycle: "running" | "completed" | "error" | ""
diff/added/removed  a file change (edit/write): the unified-diff text + counts
```

For file-changing tools the runner MAPS its runtime's native change shape onto the
neutral `diff`/`added`/`removed` (opencode `metadata.diff` + `filediff.additions/
deletions`; Claude Code `gitDiff.patch/additions/deletions`) — it never parses or
computes. The CLI formats `+added/-removed` for the one-line detail and, under
`--show-diffs`, renders `diff` as a block in the chosen `--diff-style`: `unified`
(default, one column, header noise stripped) or `split` (side-by-side old | new).
Both are built rich-only by parsing the diff (`_diff_rows`) — rich has no native
diff widget — so all layout lives in the CLI and the runner only supplies the
neutral diff string.

The CLI renders only these neutral fields — it never re-parses `raw`. `kind`
drives the base style; for tools, `status` refines the color (running=cyan,
completed=green, error=red). The CLI colors only the leading keyword and leaves
the content bare, so rich's highlighter decorates paths/numbers/strings on top:

```
analyst - step
analyst tool Edit src/app.py            (green "tool"; rich colors the path)
analyst tool Grep "Patient" (12 matches)
analyst The report has been written to …
analyst step done (12,793 tokens)
```

Why this split: "how to read the stream" belongs to the runner; "how to lay it
out and color it" belongs to the CLI; they meet on the neutral fields. A new
runtime (Claude Code, …) fills the same fields from its own stream and the
existing CLI renders it with zero changes. `raw` remains a diagnostic
passthrough (for `--show-events`), not something the neutral renderer touches.

## Plumbing the event hook through the tiers

`run_agent`'s `on_event` is the actual per-event callback. At Tier 3, the
agent name is not known until inside a node's `run`, so `build_flow` takes an
`on_event_factory` (agent name -> callback) instead — a deliberately different
name, not the same parameter renamed. It is engine plumbing, not a domain param,
and a callable is not serializable — so it is a build-time value, not a
`params` key: `build_flow(on_event_factory=<factory>)` -> `RunContext.on_event_factory`
-> `agent_node` calls it with its own agent name and passes the result to
`run_agent(on_event=...)`. (Same precedent as `run_instructions`; see
[input-plane](input-plane.md).)

## The CLI (optional `cli` extra: typer + rich)

`agent_flow.cli` provides a shared rich `Console`, `event_printer(label)` +
`render_event(ev)` (the neutral event renderer; the label is the NODE name, so a
live firehose stays navigable), `NodeProgressPrinter` (the default line-based node
progress, consuming `on_node_event`), `print_results_table(results, agents=)` (the
end-of-run Node|Agent|Outcome|Duration table), and `print_preflight_results`.
`run_cli` wires them: default = progress lines + results table; `--show-events` =
the raw firehose + results table. `--show-diffs` composes with either — it layers
edit/write diff blocks (via `render_diff`, `--diff-style` unified|split, reading the neutral
`ev.diff`) onto the default table OR the firehose. `rich`/`typer` are an OPTIONAL `cli` extra
(`pip install 'petrarca-agent-flow[cli]'`), lazy-imported inside the CLI functions (via
`utils.require_extra`), so the engine core stays render-agnostic AND import-cheap:
it emits `Event`s and `on_node_event` data and returns `NodeOutcome`s, and only
the `cli` package turns those into terminal output.

## Where it lives

`src/agent_flow/runners/base.py` (`Event` with the neutral display fields
`kind`/`title`/`detail`/`status`/`diff`/`added`/`removed`; each runtime's
`parse_event` fills them; re-exported as `agent_flow.runners.Event`),
`runners/subprocess_exec.py` (the `on_event` callback wiring in `_supervise`, Ctrl-C
process-group kill), `engine/` (`RunContext.on_event_factory`,
`build_flow(on_event_factory=, on_node_event=)`, `NodeOutcome.duration_s`), and
the `src/agent_flow/cli/` package: `events.py` (`event_printer`, `render_event`,
`render_diff`), `progress.py` (`NodeProgressPrinter`), `tables.py`
(`print_results_table`, `print_preflight_results`), `console.py` (`get_console`),
`commands/run.py` (the `run` command: flags, Ctrl-C handling), and `app.py`
(`run_cli`).
