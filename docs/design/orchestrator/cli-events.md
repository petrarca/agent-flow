---
type: Concept
title: CLI and live events
description: The Event stream and on_event hook, the --show-events one-line projection, and the optional Typer/rich CLI.
tags: [agent-flow, cli, events, on_event, rich, typer, observability]
timestamp: 2026-07-23T07:51:35Z
---

# CLI and live events

Two complementary observability channels, layered:

- **Logs (Prefect INFO)** — the always-on diagnostics: stage start/finish,
  jump-backs, concurrency setup, errors. Default level; for debugging and the
  essential record.
- **Events (`--show-events`)** — an *optional*, human-facing live view of what
  each agent is doing, rendered nicely. On top of the logs, not instead.

## The event hook (render-agnostic core)

Each runner stdout line the [supervisor](supervision.md) sees becomes an `Event`
carrying the supervision fields (`tokens`, `cost`, `is_terminal`, `is_event`) and
the **raw** original line. `run_agent` accepts an `on_event: (Event) -> None`
callback, invoked per event; supervision ignores it, and display errors are
swallowed so they can never disrupt a run.

The core does **not** interpret event content or render anything — it stays
render-agnostic. `parse_event` extracts only what supervision needs and keeps the
raw line for a consumer to display. This avoids coupling the engine to a runner's
(versioned) event schema.

## The projection (display concern, in the CLI)

Raw opencode NDJSON is far too verbose to show as-is, so the CLI projects each
event to **one readable line** using a few shallow, tolerant fields:

```
analyst - step
analyst tool write /work/tech-stack.md
analyst The report has been written to …
analyst step done (12,793 tokens)
```

- `step-start` → `- step`; `step-finish` → `step done (N tokens)`
- `tool` → `tool <name> <filePath/command>`
- `text` → the agent's message line (trimmed)
- unknown shapes → the event type, or the trimmed raw line

This projection lives in the CLI (`_project_event`), not the runner/engine, so
the engine never interprets event content. Unknown shapes fall back gracefully —
it never breaks on a new event kind.

## Plumbing the event hook through the tiers

`run_agent`'s `on_event` is the actual per-event **callback**. At Tier 3, the
agent name is not known until inside a node's `run`, so `build_flow` takes an
`on_event_factory` (agent name -> callback) instead — a deliberately different
name, not the same parameter renamed. It is engine plumbing, not a domain param,
and a callable is not serializable — so it is a **build-time** value, not a
`params` key: `build_flow(on_event_factory=<factory>)` -> `RunContext.on_event_factory`
-> `agent_node` calls it with its own agent name and passes the result to
`run_agent(on_event=...)`. (Same precedent as `shared_instructions`; see
[input-plane](input-plane.md).)

## The CLI (optional `cli` extra: typer + rich)

`agent_flow.cli` provides a shared rich `Console`, `event_printer(agent)` (builds
the actual per-event callback that prints the projection — pass its result
directly to Tier-1/2 `run_agent(on_event=...)`, or wrap it in a lambda for Tier-3
`build_flow(on_event_factory=...)`), and `print_results_table(results)` (the
end-of-run stage → outcome table). The examples use Typer commands with
`--show-events/-v`. `rich`/`typer` are core dependencies, but the engine core
stays render-agnostic: it emits `Event`s and returns status dicts, and only the
`cli` module turns those into terminal output.

## Where it lives

`src/agent_flow/runners.py` (`Event`, `parse_event`), `agent_runtime` (the
`on_event` callback), `engine.py` (`RunContext.on_event_factory`,
`build_flow(on_event_factory=...)`), `src/agent_flow/cli.py` (`event_printer`,
`_project_event`, `print_results_table`, `get_console`).
