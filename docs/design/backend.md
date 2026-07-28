---
type: Concept
title: Execution backend — in-process default, Prefect opt-in, swappable by design
description: The FlowBackend seam, the local default and Prefect opt-in backends, the candidates considered, and deployment modes.
tags: [agent-flow, backend, inprocess, prefect, swappable, deployment]
timestamp: 2026-07-23T07:51:35Z
---

# Execution backend

The backend runs the DAG the [engine](engine.md) builds: order, parallel fan-out,
retries, concurrency limits, crash resume, and an observability UI.

Requirements for *this* workload: Python-native, DAG + parallel fan-out, custom
retry conditions, concurrency limits, bounded conditional loops, crash resume, an
observability UI, finite batch runs, no human-in-the-loop mid-run, small
team.

## Candidates (researched Jul 2026)

| Option | License | Self-host infra | Durability | UI |
|---|---|---|---|---|
| **Prefect 3** | Apache-2.0 | server + Postgres (or embedded SQLite for dev) | robust scheduler | yes (server) |
| Hatchet | MIT | Postgres only (+ Go engine + worker) | durable task queue | built-in |
| DBOS | MIT | Postgres only, no server (a library) | durable execution (checkpoint + fork) | weak |
| Temporal | MIT | cluster + DB + workers | event-sourced replay (strongest) | yes |
| Dagster | Apache-2.0 | server + Postgres | robust scheduler | yes |

## The seam is real: FlowBackend (local default, Prefect opt-in)

The execution backend is a first-class abstraction, not just "Prefect behind a
lazy import". `agent_flow.backends.FlowBackend` is an `abc.ABC` with a concrete
template-method `run_group` (the solo-vs-parallel branch + failure->degraded
mapping, identical for every backend) and backend-specific primitives:
`run_session` (establish an execution context — a Prefect `@flow`, or nothing),
`_execute_parallel` (the fan-out primitive — Prefect `.submit()` or an `anyio`
task group), `apply_concurrency_limit`, `get_logger`, and `bootstrap`/`teardown`
lifecycle hooks. `get_backend(name)` resolves one.

Since the [engine](engine.md) is async-first, the execution methods are
**coroutines** — `run_group`, `run_session`, `_execute_parallel`, and
`apply_concurrency_limit` are `async def` (the engine `await`s them); the
non-blocking `get_logger` / `bootstrap` / `teardown` stay synchronous.

Two backends ship:

- **InProcessBackend (default)** — in-process: an `anyio` task group for
  parallel groups, an `anyio.Semaphore` for the LLM concurrency limit, stdlib
  logging. No Prefect, no temporary server, fast startup, one fewer heavy
  dependency at run time. This is what an everyday single run uses. A blocking
  node's `NodeBlocked` propagates out of the task group and aborts the run
  (unwrapped from anyio's `ExceptionGroup`); any other per-node error degrades
  that node; a dropped node is backfilled to `degraded` — never lost.
- **PrefectBackend (opt-in)** — the Prefect-3 behavior below, now as async
  `@task`/`@flow` (a natural fit — Prefect is async underneath): `submit()`/
  `wait()`, `get_run_logger()`, a global server-side concurrency limit, the run
  UI. The blocking future-gather is offloaded to a worker thread so it never
  stalls the engine's loop. Prefect is imported lazily inside the backend, so
  nothing is loaded unless you select it.

Select with `build_flow(nodes, backend="inprocess"|"prefect")`, the CLI
`--backend inprocess|prefect`, or `AGENT_FLOW_BACKEND`. The **engine owns all flow
logic** (plan, walk, jump-back, `start_from`/`only`, run-context); the backend
only executes. The core primitives (`run_agent`, runners) and the pure DAG
helpers (`plan_groups`/`interpret`/`_walk`) stay Prefect-free — guarded by an
import-isolation test that runs them (and a InProcessBackend flow) with `prefect`
blocked.

## Decision: Prefect 3, kept as ONE backend among the swappable set

**Why Prefect.** Best-balanced fit for finite batch, Python-native, no human
gates — and the most mature Python-native option (largest community, lowest risk
for a small team). It covers every requirement: `.submit()`/`wait()` fan-out,
`retry_condition_fn`, tag-based concurrency limits, Python conditional loops, a
persistent-server UI, and a zero-infra embedded mode for dev. Its only real
weakness — durability is "robust scheduler," not event-sourced replay — **does
not bite finite <1h runs**: you re-run a failed stage, you never replay a
multi-day workflow.

**Why not the "better on paper" options — the axis is wrong.** Temporal / DBOS
beat Prefect on *durability*, a problem this workload does not have; Temporal's
cost (cluster, determinism model, versioning) is unjustified for batch analysis.
AI-native tools (LangGraph/CrewAI) are agent *behaviour* frameworks, not durable
DAG orchestrators — they run *inside* a task, not as the engine.

**Hatchet is the strongest challenger** (MIT, Postgres-only, durable tasks,
first-class per-key rate limits, built-in UI + OTel/Prometheus), but younger and
its Python SDK is a thin client over a Go engine. For a 1–2 person team, Prefect's
maturity outweighs Hatchet's edge today — but not enough to accept lock-in.

**Swappability is first-class — and now realized.** All domain logic
(supervision, gates, re-run loops, injection, telemetry, the declared graph) is
backend-agnostic. A backend is a `FlowBackend` subclass; Prefect is one of two
shipped implementations (InProcessBackend being the other, default). Adding Hatchet
later = write one `HatchetBackend(FlowBackend)` and register it; the graphs, the
engine, and all primitives do not move.

**Exit criteria:** to Hatchet if Postgres-only ops / built-in rate limiting /
its UI become compelling or throughput exceeds Prefect's comfort zone; to DBOS
if a lighter durable model (library, no server) is wanted and its weak UI is
acceptable (its fork-from-step suits re-run loops); to Temporal only if the
workload becomes long-lived / human-gated (not this pipeline).

## Deployment modes

These modes apply to the PrefectBackend only; the InProcessBackend runs in-process
with no server and no SQLite.

- **Embedded (dev):** the PrefectBackend runs an in-process temporary server + an
  in-memory SQLite DB, torn down per run. Zero standing infra; no UI/history.
  Configured by `PrefectBackend.bootstrap()` (which owns `_prefect_env.bootstrap()`),
  invoked only when `--backend prefect` is selected.
- **File-backed (dev+):** `PREFECT_PERSIST=1` — a file-backed SQLite so runs
  survive process restart without a standing server.
- **Persistent (production):** a standing Prefect server + Postgres; the flow
  runs as a plain script pointed at it via `PREFECT_API_URL`, recording runs so
  the dashboard + history exist. A validated `deploy/docker-compose.yml`
  (Prefect server + Postgres 18) exists in the repo for this mode.

## Where it lives

- `src/agent_flow/backends/` — the seam: `base.py` (`FlowBackend` ABC),
  `inprocess.py` (`InProcessBackend`, default; named for the mechanism, not a
  location), `prefect.py` (`PrefectBackend`, lazy-import), `_prefect_env.py`
  (`bootstrap`, the three Prefect modes, owned by PrefectBackend), and
  `__init__.py` (the `get_backend(name, *, llm_tag="llm")` factory + registry).
- `src/agent_flow/engine.py` (`build_flow`) — dispatches execution through the
  selected backend; contains no Prefect code itself.
