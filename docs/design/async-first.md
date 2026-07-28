---
type: Design
title: Async-first core — migrate the engine to anyio
description: Why agent-flow's core is async-first on anyio — so async-native agent libraries are first-class in-process citizens and a flow can run inside a consumer's event loop — and how the sync facade keeps the change additive.
tags: [agent-flow, async, anyio, engine, executor, backend, pydantic-ai]
status: implemented
---

# Async-first core

## Why async

The agent-library ecosystem is async-native: PydanticAI (`await agent.run()`),
the Claude Agent SDK, and most in-process agent frameworks want an event loop.
Driving them from a sync engine costs a bridge per integration and blocks two
things:

1. async agents as first-class in-process nodes — an `async def` impl that
   `await`s the agent, no bridge;
2. embedding in a consumer's event loop — `await arun_flow(...)` from a FastAPI
   handler or a notebook, instead of thread-offloading.

The consumer-facing change is additive: sync callables and sync entry points keep
working, so the cost was internal.

## Runtime primitive: anyio

anyio rather than raw asyncio, as a core dependency:

- Native inside FastAPI/Starlette, which is itself anyio-based: `await
  arun_flow(...)` from a route composes on the same loop, with no bridging and no
  "loop already running" trap.
- Structured concurrency: parallel fan-out maps to `anyio.create_task_group()`,
  and the existing "a blocking node failure cancels its siblings" semantics fall
  out for free — a `NodeBlocked` propagating from a child cancels the group.
  `asyncio.gather` would mean hand-managing that.
- `anyio.move_on_after` and cancel scopes map directly onto the supervisor's
  idle-liveness deadline.
- `anyio.to_thread.run_sync` offloads consumer sync callables so they cannot
  stall the loop.
- `anyio.open_process` gives async subprocess handling with async stream reads,
  replacing `Popen` + threads + `queue.Queue`.

## Programming-model impact — additive, not breaking

The declarative tier is untouched; async surfaces only at consumer-supplied
callables and programmatic entry points.

Unchanged:
- `FlowDef` / `NodeDef` — pure data.
- `gate="require_file"`, `gate_args`, `depends_on`, `criticality`, `exports` —
  declarations.
- `RunConfig`, CLI flags, `run_cli(...)` — stays sync-facing (a single
  `anyio.run(...)` bridge inside).

New, all additive — sync is still accepted:
- Programmatic runs gain an async twin:
  `outcomes = run_flow(flow, ...)` (sync, `anyio.run` inside) **and**
  `outcomes = await arun_flow(flow, ...)` (native async, for loop-embedding).
- In-process impls may be `async def`:
  `agent_node(impl=async_fn)` / `NodeDef(impl_ref=...)` — engine awaits if
  awaitable, else calls directly (offloading blocking sync to a thread).
- Custom gates, hooks, exports, `on_event` and hand-written `run` nodes may be
  `async def`, with the same dispatch.

In short: write `async def` where your agent library is async, plain `def` where
it is not.

## Sync facade strategy

Async is the native core; sync entry points are thin wrappers:

- `arun_flow(...)` — native async, the real entry.
- `run_flow(...)` — `anyio.run(lambda: arun_flow(...))`. Public, unchanged
  signature for existing consumers.
- `build_flow(...)` — body stays sync (it only builds closures); the RETURNED
  pipeline callable becomes async. `run_cli` / `run_flow` bridge it with a
  single `anyio.run` at the call site.
- `run_agent(...)` (Tier-1 shim) — becomes `async def arun_agent`; keep a sync
  `run_agent` wrapper (`anyio.run`) for back-compat.

Consumer sync callables (gates/impls/hooks) that do blocking I/O are offloaded
via `anyio.to_thread.run_sync` so they never stall the loop.

## What this is NOT

- Not a change to the declarative programming model (data + `run_cli`).
- Not a break for sync consumers: sync entry points and sync callables keep
  working, via wrappers and `isawaitable` dispatch.
- Not the remote/facade work, though an async core makes an async `ServeExecutor`
  natural when that resumes.
