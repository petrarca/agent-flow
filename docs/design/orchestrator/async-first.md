---
type: Design
title: Async-first core — migrate the engine to anyio
description: Plan to migrate agent-flow's synchronous core to an async-first engine on anyio, so async-native agent libraries (PydanticAI) are first-class and the flow can run inside a consumer's event loop (FastAPI). Covers the programming-model impact (additive — sync callables still work), the layer-by-layer migration inventory, dependency ordering, risks, and the sync-facade strategy.
tags: [agent-flow, async, anyio, engine, executor, backend, pydantic-ai]
status: implemented
---

# Async-first core — migrate the engine to anyio

## Why now

The engine is synchronous top to bottom; parallelism is thread-based. But the
agent-library ecosystem is async-native: **PydanticAI** (`await agent.run()`),
the Claude Agent SDK, and most in-process agent frameworks want an event loop.
Driving them from a sync engine forces a per-integration bridge tax and blocks
the two things we actually want:

1. **async agents as first-class in-process citizens** — write an `async def`
   impl that `await`s a PydanticAI agent, no bridge;
2. **embedding in a consumer's event loop** — `await arun_flow(...)` from a
   FastAPI handler / notebook, instead of thread-offloading.

We are in **v0**: no compatibility promise, one subprocess supervisor, two
backends, ~32 test files. The consumer-facing surface change is **additive**
(sync callables keep working), so the cost is almost entirely *internal* — and
that cost only grows as more runners, the remote executor, and consumer
pipelines pile onto the sync assumption. Doing the async-first conversion now,
while the surface is small, is far cheaper than later.

## Runtime primitive: anyio

Use **anyio** (not raw asyncio) as the core concurrency primitive, added to
`[project.dependencies]` (`anyio>=4`; already present transitively via Prefect).
Rationale:

- **Native inside FastAPI/Starlette** — Starlette *is* anyio-based. `await
  arun_flow(...)` from a route composes on the same loop, no bridging, no
  "loop already running" trap. (Raw asyncio also works there, but anyio is the
  idiom the host uses.)
- **Structured concurrency (task groups)** — the parallel-node fan-out maps to
  `anyio.create_task_group()`. Crucially, our existing "a blocking-criticality
  node failure cancels its siblings and aborts the run" semantics fall out
  naturally: a `NodeBlocked` propagating out of a child cancels the group. With
  `asyncio.gather` we'd hand-manage sibling cancellation.
- **`anyio.move_on_after` / cancel scopes** — the direct map for the supervisor's
  idle-liveness deadline (see below), cleaner than `asyncio.wait_for`.
- **`anyio.to_thread.run_sync`** — the clean primitive for offloading consumer
  *sync* callables so they don't stall the loop.
- **`anyio.open_process` / `anyio.run_process`** — async subprocess with async
  stream reads, replacing `Popen` + threads + `queue.Queue`.

## Programming-model impact — additive, not breaking

The declarative tier is untouched; async surfaces only at consumer-supplied
callables and programmatic entry points.

**Unchanged:**
- `FlowDef` / `NodeDef` — pure data.
- `gate="require_file"`, `gate_args`, `depends_on`, `criticality`, `exports` —
  declarations.
- `RunConfig`, CLI flags, `run_cli(...)` — stays sync-facing (a single
  `anyio.run(...)` bridge inside).

**New / widened (all additive — sync still accepted):**
- **Programmatic run** gains an async twin:
  `outcomes = run_flow(flow, ...)` (sync, `anyio.run` inside) **and**
  `outcomes = await arun_flow(flow, ...)` (native async, for loop-embedding).
- **In-process impls** may be `async def`:
  `agent_node(impl=async_fn)` / `NodeDef(impl_ref=...)` — engine awaits if
  awaitable, else calls directly (offloading blocking sync to a thread).
- **Custom gates / hooks / exports / `on_event` / hand-written `run` nodes** may
  be `async def` — same sync-or-async dispatch.

The consumer "feel": **write `async def` where your agent library is async,
plain `def` where it isn't; agent-flow does the right thing either way.**

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

## Migration inventory (layer by layer)

Legend: **M** = mechanical (signature + `await`). **S** = structural (rewrite
the concurrency model).

### 1. Subprocess supervisor — `core/agent_runtime.py` — RISKIEST, fully S

The deepest rewrite: a hand-rolled thread+queue liveness loop around
`subprocess.Popen`.

| Current | → anyio |
|---------|---------|
| `_start_line_reader` / `_start_stderr_reader`: `threading.Thread` + `queue.Queue`, blocking `for line in proc.stdout` | reader tasks in a task group, pushing to `anyio.create_memory_object_stream()` |
| `subprocess.Popen(..., text=True, start_new_session=True)` | `await anyio.open_process(...)`. **Footgun:** anyio returns BYTES streams — reimplement `text=True` line framing (decode + split on `\n`) |
| `_supervise_loop`: `time.monotonic()` deadline + `queue.get(timeout=…)` poll; deadline reset on each real event | per-wait `anyio.move_on_after(idle_timeout_s)` around `receive()`; fresh timeout per wait = "reset on activity"; inspect `scope.cancelled_caught` for the stale branch. Sidecar-appeared early-stop + EOF still short-circuit |
| `_reap`: `proc.wait(timeout=5)` | `with anyio.move_on_after(5): await proc.wait()` |
| `_kill_group`: `os.killpg` + `proc.wait(timeout=5)` | `os.killpg` stays (non-blocking syscall); the wait becomes `move_on_after` **inside a shielded `anyio.CancelScope(shield=True)`** so kill completes even under cancellation |
| `KeyboardInterrupt` handling in `_supervise` | catch `KeyboardInterrupt` AND `anyio.get_cancelled_exc_class()`; shielded group kill |
| `_drain_stderr`: `queue.get_nowait()` | drain the stderr memory-stream (`receive_nowait` until `WouldBlock`/`EndOfStream`) |
| `_apply_event`/`_consume_line`/`_stop_kind`/`_finish`/`_no_verdict_reason` | **pure — stay sync** |
| `run_agent(...)` public shim | `async def arun_agent`; sync `run_agent` wrapper |

**Correctness properties to preserve:** (a) deadline resets only on *real*
events, not noise; (b) sidecar-on-disk stops promptly even while the process
lingers; (c) cancel/Ctrl-C ALWAYS reaps the whole process group (no orphaned
opencode + MCP children); (d) the EOF vs stale vs sidecar completion-kind
distinction. The integration tests (`test_run_agent_*`) are the gate.

### 2. Executor seam — `runners/executor.py` + 3 impls

- `AgentExecutor.run` (abstract) → `async def run`. **S** (contract).
- `assemble_result`, `check_content_status` — pure, **stay sync**.
- `SubprocessExecutor.run` → async (see §1). **S**.
- `InProcessExecutor.run` → async; support BOTH sync and async impls:
  `raw = self.impl(inv); if isawaitable(raw): raw = await raw`. `adapt_result`
  stays sync. **S** wrapper.
- `MockExecutor.run` → async; support sync/async behaviour; tiny sidecar write
  stays sync (or `anyio.Path`). `_coerce_envelope` stays sync. **S**.

### 3. Engine — `engine.py`

**Pure — stay sync:** `plan_groups`, `_toposort`, `_group_membership`,
`_group_dependencies`, all name→index resolvers, `_pick_jump_back`,
`_instruction_for_target`, `_resolve_gate`, dataclasses.

**Become async (call node.run / gates / hooks / exports / backend):**
`interpret` (the gate `while True` loop; `await node.run(ctx)`; await-if-awaitable
for gate/hooks/exports) → `run_node` closure → `run_group` runner → `_walk` →
`_walk_session` → the returned `_pipeline` callable. `build_flow` body stays
sync; only its returned callable is async. Add an `isawaitable`-dispatch wrapper
at each consumer-callable call site (gates, hooks, exports, `on_node_event`).
**S** throughout the call chain.

### 4. Backend seam — `backends/`

- `FlowBackend.run_group` / `run_session` / `_execute_parallel` /
  `apply_concurrency_limit` → async. `get_logger` / `bootstrap` / `teardown`
  can **stay sync** (non-blocking).
- **InProcess `_execute_parallel`**: `ThreadPoolExecutor` → `anyio.create_task_group()`;
  `threading.Semaphore` → `anyio.Semaphore`. Preserve: `NodeBlocked` propagates
  and cancels the group (aborts the run); any other exception → degraded; never
  drop a node. **S** (flagship threadpool→task-group rewrite, second-riskiest).
- **Prefect**: async `@task`/`@flow` (Prefect is async underneath — a *better*
  fit post-migration); `apply_concurrency_limit` already writes `async def
  _create()` + `anyio.run` → simplify to `await _create()`. **S** but native.

### 5. `node_builder.py`

Factory stays sync; the inner `run(ctx)` closure → `async def`, single await
point `await executor.run(inv)`. Prompt composition / small-file reads stay sync
inline. **S** (one await point).

### 6. Types — `engine.py` / `gates.py`

- `RunFn` → `Callable[[RunContext], Awaitable[Any]]` (accept both via
  `isawaitable`). `Gate`, `exports` widened to allow async. Built-in gates stay
  sync (dispatch handles both). Dataclasses unchanged.

### 7. CLI — `cli/`

Stays sync. Single `anyio.run(...)` bridge at the `pipeline(**kwargs)` call in
`commands/run.py`. Typer commands, preflight, printers stay sync. `on_event` /
`on_node_event` printers stay sync (fast `console.print`, called inline from
async — keep the existing guard). `flowdef/compile.run_flow` gets the same
bridge; add `arun_flow`.

### 8. Other blocking I/O

- `opencode.py` `_run_text` / `_opencode_debug_config` (subprocess for
  `--version` / `debug config`): diagnostic, off the hot path — leave sync or
  `anyio.run_process` if `info()` goes async. **Lowest priority.**
- `preflight.py`, `core/env.py load_env`, `build_control_preamble`,
  `read_context_blocks` (small files), `_read_sidecar` — pre-run or tiny —
  **stay sync** (optionally `anyio.Path` later).
- `run_context.py` `threading.Lock` — held briefly, correct from async too —
  **keep** (revisit only if we want `async def update`).

### 9. Tests

- `conftest.py` `StubRunner` — pure runner, **unchanged**.
- Add the anyio pytest plugin (`pytest.mark.anyio`); mark async tests; sync
  tests calling the flow go through an `anyio.run(...)` helper.
- ~17–19 of 32 files touched (those invoking `executor.run`, `interpret`,
  `run_agent`, `pipeline`, `_execute_parallel`, `run_flow`). Pure tests
  (`test_context`, `test_schema`, `test_preflight`, `test_report_signals`,
  `test_control_protocol`, `test_utils`, `test_run_config`, …) unaffected.

## Dependency ordering (bottom-up)

Convert strictly in this order — each layer's async signature is a prerequisite
for the one above. Keep the suite green at each stage; each stage is
independently committable.

1. Add `anyio>=4` to core deps; add `pytest.mark.anyio` setup.
2. **`core/agent_runtime.py`** — supervisor + `SubprocessExecutor.run` + `arun_agent`. **RISKIEST.** Get `_kill_group` shielding + cancellation → process-group kill exactly right first; integration tests gate it.
3. **`runners/executor.py` ABC** + **`inprocess.py`** + **`mock_exec.py`** (`run` async; sync/async impl support; pure helpers stay sync).
4. **`node_builder.py`** — inner `run` closure async.
5. **`engine.py`** — `interpret` → `run_node` → `run_group` → `_walk` → `_walk_session` → `_pipeline`; await-if-awaitable for consumer callables.
6. **`backends/base.py`** → **`inprocess.py`** (threadpool → task group) → **`prefect.py`** (async task/flow). **Second-riskiest.**
7. **CLI** + **`flowdef.run_flow`/`arun_flow`** — the single `anyio.run` bridge.
8. **Tests/conftest** — mark async, add helpers.
9. Examples + docs; PydanticAI adapter (the payoff — a trivial `async def` impl awaiting `agent.run()`).

## Risks, ranked

1. **Supervisor liveness + `_kill_group` cancellation** — subtle deadline-reset
   semantics, EOF/stale/sidecar distinction, and the hard requirement that
   cancel/Ctrl-C always reaps the process group. anyio's structured cancellation
   changes the interruption path.
2. **InProcess threadpool → task group** — exception routing (blocking→abort via
   cancellation; others→degraded; never drop a node) + semaphore semantics.
3. **anyio `open_process` returns bytes** — reimplement `text=True` line framing;
   a footgun for the event parser.
4. **Sync-vs-async consumer callables** — engine must accept BOTH at every call
   site (gates, exports, hooks, impl, on_event, on_node_event) or it breaks
   every existing consumer; `isawaitable`-dispatch.
5. **Public API sync→async** — `run_agent`, `build_flow`'s returned callable,
   `run_flow`. Decide the sync-shim wrapper strategy up front (this doc:
   `anyio.run`-based sync shims + async natives).

## What this is NOT

- Not a change to the declarative programming model (data + `run_cli`).
- Not a break for existing sync consumers (sync entry points + sync callables
  keep working via wrappers + `isawaitable` dispatch).
- Not the remote/facade work (parked) — though an async core makes an async
  `ServeExecutor` (HTTP via anyio/httpx) natural when that resumes.
