---
type: Usage Overview
title: Using agent-flow — for consumers of the library
description: Task-oriented documentation for building your own pipeline on agent-flow. Start here.
tags: [agent-flow, usage, guide, getting-started, consumer-documentation]
timestamp: 2026-07-23T08:54:40Z
---

# Using agent-flow

This is the **consumer-facing** documentation: how to build your own pipeline on
top of `agent-flow`. It is task-oriented — install it, write your first
pipeline, write agents that work with it, and look up a recipe when you need
something specific.

For **why** the library is built the way it is (the three-tier architecture,
the control-file contract, gates, the engine internals), see the design bundle:
[`docs/design/orchestrator/index.md`](../design/orchestrator/index.md). This
bundle assumes you just want to *use* it and links out to the design docs only
where the "why" genuinely helps.

## Guides

| Guide | Read this when… |
|---|---|
| [getting-started.md](getting-started.md) | You're starting from zero: install, write your first pipeline, run it. |
| [writing-agents.md](writing-agents.md) | You're writing the opencode agent `.md` files agent-flow will supervise. |
| [recipes.md](recipes.md) | You need a specific thing on the FlowDef surface: a re-run loop, parallel steps, custom logic, typed output. |
| [advanced-recipes.md](advanced-recipes.md) | You're working at the lower level: `agent_node`/`build_flow`, gate callables, your own flow, dropping a tier. |

## The 30-second orientation

`agent-flow` supervises coding-agent subprocesses (opencode) and runs them as a
declared graph. You write:

1. **Agent `.md` files** — the actual work (analyze, write a report, verify
   something). See [writing-agents.md](writing-agents.md).
2. **A pipeline declaration** — `agent_node(...)` per agent, wired with
   `depends_on` and optional `gate`s. See [getting-started.md](getting-started.md).
3. **Nothing else** — prompts, sidecar files, supervision, retries, and
   parallelism are the library's job.

## Layering (high level → low level)

Three tiers; each is usable on its own. Most consumers use Tier 3.

```
TIER 3  DECLARATIVE      declare Nodes -> build_flow() -> a runnable flow callable
  (most declarative)     agent_node() = one call per agent
        │ composes
TIER 2  PRIMITIVES       call run_agent() as the leaf of YOUR OWN flow
        │ uses
TIER 1  ENGINE CORE      run_agent(): spawn + liveness-supervise + kill + sidecar verdict
  (closest to the metal) runner-agnostic; no Prefect
        │ invokes
        AGENT RUNTIME    opencode agents (.md) — external, unchanged
```

**The engine owns the flow logic and dispatches execution to a swappable
backend.** At Tier 3, `build_flow` compiles your graph into a runnable flow
callable that runs on the selected backend: the in-process `InProcessBackend`
(default) or the opt-in [Prefect](https://www.prefect.io/) backend (parallel
fan-out, concurrency limits, a run UI). The backend is a seam — Prefect is
imported only when you choose it, and Tiers 1–2 don't require it. Details and the
backend rationale:
[`design/orchestrator/backend.md`](../design/orchestrator/backend.md).

**Async-first, sync-friendly.** The engine core runs on
[`anyio`](https://anyio.readthedocs.io/), so an async-native agent (PydanticAI)
is a first-class in-process node and the flow can run inside your own event loop.
The change is additive: the flow callable `build_flow` returns is an async
coroutine, but the entry points you call keep their blocking signatures —
`run_flow(flow, …)` and `run_cli(flow)` bridge it with `anyio.run` for you. From
inside an event loop, use the async twins `await arun_flow(flow, …)` /
`await build_flow(nodes)(…)` instead. Consumer callables (impls, gates, exports,
hooks) may be sync `def` or `async def` — the engine awaits an awaitable and
offloads a blocking sync callable to a worker thread. See
[`design/orchestrator/engine.md`](../design/orchestrator/engine.md).

```python
import anyio
from agent_flow import agent_node, build_flow

nodes = [agent_node("hello", "hello-agent",
                    inputs={"REPORT": "{run_dir}/hello.md"},
                    gate_ref="require_file", gate_args={"path": "{run_dir}/hello.md"})]
flow = build_flow(nodes, name="hello")           # -> an async flow callable
anyio.run(lambda: flow(runtime="opencode"))      # no run_dir -> a temp dir (logged)
# on an event loop you'd instead:  await flow(runtime="opencode")
```
