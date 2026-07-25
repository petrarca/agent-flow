---
type: Concept
title: Gates — the consumer's flow-control hook
description: Directives (Continue/Restart/GoTo/Stop), GateContext, and ready-made gates for the common checks.
tags: [agent-flow, gates, directives, flow-control, consumer-hook]
timestamp: 2026-07-23T07:51:35Z
---

# Gates

A **gate** is the consumer's optional hook: after an agent runs, it inspects what
the agent produced (files on disk, the control `result`) and returns a
**directive** that steers the flow. This is the seam that keeps domain knowledge
— "what a report is, when a re-run is needed" — out of the engine.

A gate is any callable `(GateContext) -> Directive`. A node with **no** gate
behaves as if it returned `Continue()` — absent means continue.

## Directives (the closed set)

| Directive | Effect |
|---|---|
| `Continue()` | proceed to the next node (the default). |
| `Restart(note="")` | re-run THIS node's agent, bounded by `max_cycles`. |
| `GoTo(node, note="")` | resume the flow at a named node — self (= Restart) or an **earlier** node (bounded cross-node jump-back, see [engine](engine.md)). |
| `Stop(reason="")` | abort the whole pipeline (e.g. a blocking-criticality failure). |

The closed union makes flow control discoverable and typo-proof: a reader sees
four outcomes and understands the whole vocabulary.

## `GateContext`

```python
GateContext(
    obj,       # the VALIDATED typed result object (pydantic instance) when the node
               #   set a result_schema, else None — read ctx.obj.field directly
    result,    # the RAW control envelope (status/result/telemetry) — for the no-schema
               #   case or the envelope fields
    node,      # the node that just ran (name + whatever the consumer's node type carries)
    run_dir,   # the run's artifact dir — a gate stats files under here
    cycles,    # how many times this node has re-run, so the gate can bound itself
    params,    # the pipeline's run-time params (same dict RunContext.params carries)
)
```

Prefer **`ctx.obj`** whenever the node declared a `result_schema`: it is the
validated pydantic instance, so `ctx.obj.ready` reads the structured result
cleanly — no digging a magic key out of `result`. `ctx.obj` is `None` when there
is no schema; fall back to `ctx.result` then.

`result` and `node` are typed `Any` on purpose — the library does not dictate the
shape of either, so any pipeline's node/result works. `params` is included
(default `{}`) so a gate can resolve a `{name}` template to the SAME run-time
value the node's `run` saw — a node's static declaration has no such value
available (e.g. a report path that depends on `product_key`, known only at run
time).

## Ready-made gates (optional conveniences)

The two checks almost every pipeline writes, shipped so you don't hand-roll a
closure:

```python
from agent_flow.gates import require_file, rerun_on_signal

# "did the agent actually produce its artifact?" -> Restart if missing (bounded).
# May template run params: require_file("{product_key}-report.md").
gate = require_file("tech-stack.md")

# "did this (verifier) node signal a re-run of an earlier node?" -> GoTo(target).
gate = rerun_on_signal(target="tech-stack")
```

`require_file` reads `produced()`, resolving `{name}` in its path argument
against `ctx.params`; `rerun_on_signal` reads the node's sidecar
`rerun_required` and returns `GoTo(target)` when set. Both are ordinary gates you
may use, wrap, compose, or ignore.

**A real agent must be TOLD `rerun_required` exists to use it.** The injected
[control-file protocol](control-file.md) mentions the field, but a specific
agent only knows *when* to set it if its own `.md` says so explicitly — see
`examples/tech_assessment/.opencode/agent/*-verifier.md` for the pattern.

## Why gates, not engine logic

Deciding *what to do* about an agent's output is a consumer concern, and it is
where domain knowledge lives. Putting it in gates (rather than engine flags) keeps
the engine a pure DAG/directive interpreter and lets a gate use anything —
stat files, read the control dict, inspect telemetry — to reach a decision. It
also cleanly expresses "verifier re-runs analyst" as *edge + gate*, with no
built-in pairing (see [engine](engine.md) jump-back).

## Where it lives

`src/agent_flow/gates.py` (`Directive`, `Continue`/`Restart`/`GoTo`/`Stop`,
`GateContext`, `Gate`, `require_file`, `rerun_on_signal`).
