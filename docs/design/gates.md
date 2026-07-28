---
type: Concept
title: Gates — the consumer's flow-control hook
description: Directives (Continue/Restart/GoTo/Stop), GateContext, and ready-made gates for the common checks.
tags: [agent-flow, gates, directives, flow-control, consumer-hook]
timestamp: 2026-07-23T07:51:35Z
---

# Gates

A gate is the consumer's optional hook: after an agent runs, it inspects what
the agent produced (files on disk, the control `result`) and returns a
**directive** that steers the flow. This is the seam that keeps domain knowledge
— "what a report is, when a re-run is needed" — out of the engine.

A gate is a callable `(ctx, **config) -> Directive`, where the node's `gate_args`
supply the config (bound with `functools.partial` at resolve time, so after
binding the engine calls it with just `ctx`). A gate with no per-node config is
simply `(ctx) -> Directive`. A node with no gate behaves as if it returned
`Continue()` — absent means continue.

## Directives (the closed set)

| Directive | Effect |
|---|---|
| `Continue()` | proceed to the next node (the default). |
| `Restart(instruction="")` | re-run THIS node's agent, bounded by `max_cycles`. The optional `instruction` is injected verbatim into the target's next run. |
| `GoTo(node, instruction="")` | resume the flow at a named node — self (= Restart) or an earlier node (bounded cross-node jump-back, see [engine](engine.md)). |
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
    agent_dir, # the agent-definitions dir for the just-run node (mirrors RunContext.agent_dir),
               #   provided for symmetry — usually unneeded by a gate
)
```

Prefer `ctx.obj` whenever the node declared a `result_schema`: it is the
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

The three checks almost every pipeline writes, shipped and pre-seeded into every
`FlowRegistry` so you don't hand-roll a closure. They are `(ctx, **config)` gates
referenced BY NAME + `gate_args` (or `gate_ref`/`gate_args` on `agent_node`), not
factories you call:

```python
# "did the agent actually produce its artifact?" -> Restart if missing (bounded).
# path may template run params, e.g. "{product_key}-report.md".
NodeDef(name="tech-stack", agent="tech-stack-analyst",
        gate="require_file", gate_args={"path": "{run_dir}/tech-stack.md"})

# "did this (verifier) node signal a re-run of an earlier node?" -> GoTo(target).
NodeDef(name="verify", agent="tech-stack-verifier", depends_on=["tech-stack"],
        gate="rerun_on_signal", gate_args={"target": "tech-stack"})

# same signal, but route to WHICHEVER node the verdict names -> GoTo(named).
NodeDef(name="coherence", agent="coherence-check", gate="rerun_on_named")
```

Their signatures are `require_file(ctx, *, path, on_missing=None)`,
`rerun_on_signal(ctx, *, target)`, and `rerun_on_named(ctx)`.

`require_file`: `path` is resolved via `{param}` templating against `ctx.params`
(same values the node's `run` saw); `{run_dir}` is always available. A bare
filename without a leading `/` or `{run_dir}` (e.g. `"report.md"`) is treated as
**relative to `run_dir`** — it is joined onto `ctx.run_dir`, NOT onto the process
working directory. `run_dir` is the artifact directory for the run (passed as
`run_dir=` to `run_flow`/`build_flow`; defaults to a temp dir). Use the explicit
`"{run_dir}/report.md"` form to make the intent obvious and keep `path=` visually
consistent with the node's `inputs={"REPORT": "{run_dir}/report.md"}` value.
`require_file` is a genuine FILESYSTEM check of the agent's WORK PRODUCT (the
artifact it was told to write) — distinct from the re-run gates below, which read
the VERDICT.

`rerun_on_signal` and `rerun_on_named` read `rerun_required` from the **harvested
control envelope** (`ctx.result`) — NOT from a file. By the time a gate runs, the
executor has already harvested the verdict however it came back (subprocess
sidecar, or a remote runner's own mechanism), so the gate reads the dict it was
handed and never re-reads a file or reconstructs a path. `rerun_on_signal`
returns `GoTo(target)` for a FIXED target when the field is set; `rerun_on_named`
routes to the node it NAMES (first valid backward target). All three
auto-populate the directive's `instruction`. They are ordinary gates you may
use, wrap, compose, or ignore.

**A real agent must be TOLD `rerun_required` exists to use it.** The injected
[control-file protocol](control-file.md) mentions the field, but a specific
agent only knows *when* to set it if its own `.md` says so explicitly — see
`examples/.opencode/agent/*-verifier.md` for the pattern.

## Why gates, not engine logic

Deciding *what to do* about an agent's output is a consumer concern, and it is
where domain knowledge lives. Putting it in gates (rather than engine flags) keeps
the engine a pure DAG/directive interpreter and lets a gate use anything —
stat files, read the control dict, inspect telemetry — to reach a decision. It
also cleanly expresses "verifier re-runs analyst" as *edge + gate*, with no
built-in pairing (see [engine](engine.md) jump-back).

## Where it lives

`src/agent_flow/gates.py` (`Directive`, `Continue`/`Restart`/`GoTo`/`Stop`,
`GateContext`, `Gate`, `require_file`, `rerun_on_signal`, `rerun_on_named`).
The `produced` / `rerun_targets` helpers the gates read live in
`agent_flow.core` (report_signals).
