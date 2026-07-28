---
type: Concept
title: The engine — declarative graph to a runnable flow
description: Node, plan_groups, build_flow; DAG ordering and parallel groups; bounded re-runs and cross-node jump-back.
tags: [agent-flow, engine, node, build_flow, dag, jump-back, backend]
timestamp: 2026-07-23T07:51:35Z
---

# The engine (Tier 3)

The engine compiles a graph of `Node`s into a runnable flow callable that
dispatches execution to the selected backend. It is deliberately **agnostic to
what a node does**: a `Node` carries a `run` callable and an optional
[gate](gates.md); the engine only orchestrates order, parallelism, criticality,
bounded re-runs, and cross-node jump-back. Domain knowledge lives in `run` and
`gate`, never in the engine.

The engine is backend-agnostic: it owns all flow logic (`plan_groups`,
`interpret`, `_walk`, jump-back, `start_from`, `only`, run-context) and hands the
backend a `run_node` closure. The backend (InProcessBackend by default, PrefectBackend
opt-in) supplies only execution mechanics — parallel fan-out, concurrency limit,
logger, and bootstrap/teardown.

The engine does not import Tier 1 — it meets `run_agent` only through the
caller-supplied `Node.run`. (The [node builder](node_builder.md) module is the one
place that bridges both, on purpose.)

## Async-first

The engine core is async (on [`anyio`](https://anyio.readthedocs.io/)): the
call chain `interpret → run_node → run_group → _walk → _walk_session → the flow
callable` is a chain of coroutines, and the callable `build_flow` returns is an
`async def`. This makes an async-native agent (PydanticAI) a first-class
in-process node and lets the flow run inside a consumer's event loop.

The change is additive — every consumer callable may be sync `def` OR async
`async def`, and the engine dispatches through a single `await`-if-awaitable point
(`_maybe_await`): a node's `run`, a `gate`, an `exports` impl, and the observing
hooks. A sync callable's plain return passes straight through; an async one is
awaited. The pure graph logic (`plan_groups`, the toposort, name→index resolvers,
jump-back selection) stays synchronous — there is nothing to await there.

Entry points stay sync-facing: `run_flow` / `run_cli` / `run_agent` are thin
`anyio.run` wrappers over the async natives `arun_flow` / `arun_agent` / the async
flow callable. Call the sync form from a script; `await` the async form from an
event loop (FastAPI, a notebook).

## `Node`

```python
Node(
    name,                      # unique node id
    run,                       # RunFn: (RunContext) -> anything the gate inspects; sync OR async
    gate=None,                 # optional flow-control gate; absent = Continue
    depends_on=(),             # DAG edges (upstream node names)
    parallel_group=None,       # nodes sharing a group name fan out concurrently
    criticality="blocking",    # "blocking" -> failure/Stop halts; "degrade" -> continue
    max_cycles=1,              # bound on Restart / self-GoTo / jump-back re-runs
    result_schema=None,        # optional typed-output schema (see result-schema.md)
)
```

`RunContext` (what `run` receives): `node`, `run_dir` (the run's directory —
control files + relative-path base; NOT a cwd and NOT where agents live),
`cycles`, `params` (domain inputs, threaded from the flow call), and the
engine-plumbing fields `agent_dir` (default dir where agent definitions live →
opencode `--dir`), `on_event_factory`, `run_instructions` (the flow's standing
brief), `run_additional_instructions` (this run's `-i` addition), `run_context`,
`node_overrides` (per-node run config `{node: {instructions, model, agent_dir,
duration, idle_timeout_s, options}}`, e.g. from `--instruct`/config), `durations`
(the duration-name → seconds vocabulary), `options` (run-wide runtime flags), and
`one_time_instruction` (the single-attempt instruction a gate's `Restart`/`GoTo`
delivers to the target node's next run) — all typed, not `params` keys — see
[input-plane](input-plane.md) and [cli-events](cli-events.md)). `on_event_factory`
is deliberately named differently from `run_agent`'s Tier-1 `on_event` callback
— at Tier 3 the agent name is not known until inside a node's `run`, so this is
a factory (agent name -> callback), not the callback itself.

## `plan_groups` — DAG ordering + parallel grouping (pure)

`plan_groups(nodes)` buckets nodes by `parallel_group` (solo nodes are their own
group), topologically sorts the groups by `depends_on` (Kahn, tie-broken by
declaration order), and raises on an unknown dependency or a cycle. It is pure
and unit-tested without Prefect.

## `interpret` — one node to completion

`interpret(node, …)` is a coroutine (`await interpret(...)`). It `await`s the
node's `run`, `await`s its gate, and applies the directive. Its logic is
otherwise unchanged — only the consumer callables are awaited-if-awaitable:

- `Continue` → done.
- `Restart` (and `GoTo` to self) → re-run the node in place, bounded by
  `max_cycles`.
- `Stop` → raise `NodeBlocked` (halts the run).
- `GoTo(other)` → return a `NodeOutcome(status, goto=other)` for the walker to
  act on (see jump-back below). `interpret` does not handle cross-node jumps
  itself — that requires the group sequence, which only the walker owns.

Errors raised by `run` are mapped to the node's criticality by an `on_error`
callback: `blocking` → `NodeBlocked`; `degrade` → status `degraded`.

## `build_flow` — compile to a runnable flow callable

`build_flow(nodes, *, name, llm_tag, llm_concurrency, on_event_factory, on_node_event, run_instructions, run_additional_instructions, run_context, agent_dir, node_overrides, durations, options, backend="inprocess", registry=None)`
returns an async callable `async f(run_dir="", start_from="", only="", **params) -> dict[str, NodeOutcome]`
(`build_flow`'s own body stays sync — only the returned callable is a coroutine;
`await` it, or use the sync `run_flow` / `run_cli` wrappers that bridge it with
`anyio.run`). It:

- fails fast at build time on cycles/unknown deps (`plan_groups`),
- resolves the selected `backend` (default `"inprocess"`) and dispatches each
  group's execution to it (solo inline; parallel fan-out via the backend),
- honors bounded cross-node jump-back,
- honors `start_from` (enter at a group, run forward) and `only` (run exactly
  one group, stop),
- applies an optional LLM concurrency limit on `llm_tag` (an `anyio.Semaphore` on
  the in-process backend; a server-side limit on the Prefect backend).

The backend is resolved lazily inside `build_flow` (via
`agent_flow.backends.get_backend`) so the engine module imports without pulling
any backend, keeping Prefect optional.

## Cross-node jump-back (the walker)

The walker (`_walk`) owns the planned group order and the results/jump state, so
it — not `interpret` — implements backward `GoTo`. When a group's node returns
`GoTo(target)` for an earlier group, the walker rewinds to the target's group
and re-runs from there, bounded per target by the target node's `max_cycles`.
Forward or unknown targets are ignored with a log (a gate mistake fails visibly,
not silently). This is how "a verifier node decides an earlier analyst node must
re-run" is realized — not a built-in analyst/verifier pairing, just an edge plus
a gate returning `GoTo`.

```
A ─► B(depends A). B's gate returns GoTo("A") once →
    run order: A, B, (jump-back) A, B   — then done (A hit max_cycles).
```

## Where it lives

`src/agent_flow/engine.py` (`Node`, `RunContext`, `NodeOutcome`, `plan_groups`,
`interpret`, `build_flow`, `_walk`).
