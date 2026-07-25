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

The engine does **not** import Tier 1 — it meets `run_agent` only through the
caller-supplied `Node.run`. (The [node builder](node_builder.md) module is the one
place that bridges both, on purpose.)

## `Node`

```python
Node(
    name,                      # unique node id
    run,                       # RunFn: (RunContext) -> anything the gate inspects
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
opencode `--dir`; per-node override on `agent_node`), `on_event_factory`, and
`shared_instructions` (all typed, not `params` keys — see
[input-plane](input-plane.md) and [cli-events](cli-events.md)). `on_event_factory`
is deliberately named differently from `run_agent`'s Tier-1 `on_event` callback
— at Tier 3 the agent name is not known until inside a node's `run`, so this is
a factory (agent name -> callback), not the callback itself.

## `plan_groups` — DAG ordering + parallel grouping (pure)

`plan_groups(nodes)` buckets nodes by `parallel_group` (solo nodes are their own
group), topologically sorts the groups by `depends_on` (Kahn, tie-broken by
declaration order), and raises on an unknown dependency or a cycle. It is pure
and unit-tested without Prefect.

## `interpret` — one node to completion (pure)

`interpret(node, …)` runs the node, invokes its gate, and applies the directive:

- `Continue` → done.
- `Restart` (and `GoTo` to self) → re-run the node in place, bounded by
  `max_cycles`.
- `Stop` → raise `NodeBlocked` (halts the run).
- `GoTo(other)` → return a `NodeOutcome(status, goto=other)` for the **walker** to
  act on (see jump-back below). `interpret` does not handle cross-node jumps
  itself — that requires the group sequence, which only the walker owns.

Errors raised by `run` are mapped to the node's criticality by an `on_error`
callback: `blocking` → `NodeBlocked`; `degrade` → status `degraded`.

## `build_flow` — compile to a runnable flow callable

`build_flow(nodes, *, name, llm_tag, llm_concurrency, on_event_factory, on_node_event, shared_instructions, shared_context, agent_dir, node_instructions, backend="inprocess")`
returns a plain callable `f(run_dir="", start_from="", only="", **params) -> dict[str, NodeOutcome]`.
It:

- fails fast at build time on cycles/unknown deps (`plan_groups`),
- resolves the selected `backend` (default `"inprocess"`) and dispatches each
  group's execution to it (solo inline; parallel fan-out via the backend),
- honors bounded **cross-node jump-back**,
- honors `start_from` (enter at a group, run forward) and `only` (run exactly
  one group, stop),
- applies an optional LLM concurrency limit on `llm_tag` (a process-local
  semaphore on the in-process backend; a server-side limit on the Prefect backend).

The backend is resolved **lazily inside `build_flow`** (via
`agent_flow.backends.get_backend`) so the engine module imports without pulling
any backend, keeping Prefect optional.

## Cross-node jump-back (the walker)

The walker (`_walk`) owns the planned group order and the results/jump state, so
it — not `interpret` — implements backward `GoTo`. When a group's node returns
`GoTo(target)` for an **earlier** group, the walker rewinds to the target's group
and re-runs from there, **bounded per target by the target node's `max_cycles`**.
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
