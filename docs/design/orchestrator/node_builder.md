---
type: Concept
title: Node builder — agent_node, the one-call node
description: The agent_node factory for the common 'run one agent' case, and the Tier-3 developer experience.
tags: [agent-flow, node_builder, agent_node, developer-experience, tier-3]
timestamp: 2026-07-23T07:51:35Z
---

# Node builder: `agent_node`

Tier 3's `Node` takes a `run` callable — maximally flexible, but it means a
consumer would hand-write prompt-building, control-path derivation, and the
`run_agent` call for every node. The overwhelmingly common shape is simply *"run
one runtime agent, hand it a work order, point it at a control file, get the
result."* `agent_node(...)` builds exactly that node in **one call**.

It is a convenience, not a new layer: it returns a plain `Node`, so it mixes
freely with hand-written `run` callables in the same graph. It is the one module
that depends on BOTH the [engine](engine.md) (`Node`) and Tier 1 (`run_agent`),
keeping `engine.py` itself decoupled from the runtime.

## Signature

```python
agent_node(
    name, agent, *,
    inputs=None,             # KEY: value work order, templated (see input-plane.md)
    depends_on=(), parallel_group=None, criticality="blocking",
    instructions="",         # per-node instruction block, additive (see input-plane.md)
    context=(),              # per-node context SOURCES, content injected (see input-plane.md)
    gate=None,               # optional consumer gate (see gates.md)
    result_schema=None,      # optional typed output (see result-schema.md)
    max_cycles=1,
    model=None, idle_timeout_s=None,
    agent_dir=None,          # per-node override of where agent definitions live
    exports=None,            # optional result->params publish hook
) -> Node
```

`agent` is the runtime agent name (e.g. an opencode `--agent`). It is
**domain-neutral** — the library attaches no meaning to it.

## No analyst/verifier concept

There is deliberately **no** notion of "analyst"/"verifier" in the library. A
verifier is just **another `agent_node`** that `depends_on` its subject and
carries a `rerun_on_signal(target=...)` [gate](gates.md); the engine's bounded
cross-node [jump-back](engine.md) drives the re-run. Any node can route flow to
any upstream node — the library imposes no adjacency.

```python
nodes = [
    agent_node("tech-stack", "tech-stack-analyst",
               inputs={"REPORT": "{run_dir}/tech-stack.md"},
               gate=require_file("tech-stack.md")),
    agent_node("tech-stack-verify", "tech-stack-verifier",
               depends_on=("tech-stack",), criticality="degrade",
               gate=rerun_on_signal(target="tech-stack")),
]
```

## What it does for you

Inside the generated `run`, `agent_node`:

- renders the `inputs` into a `KEY: value` work order, expanding `{name}`
  templates against the flow `params` (plus `{run_dir}`),
- prepends the per-node `instructions` block (if any),
- derives the per-node control-sidecar path (`<name>.control.json`),
- calls `run_agent` with the resolved runner (`params["runtime"]`), model, idle
  timeout, `result_schema`, the run-wide `shared_instructions`, and a per-agent
  `on_event` callback built from `RunContext.on_event_factory` (if set),
- returns the control envelope plus a little `_`-prefixed telemetry for the gate.

The Tier-3 payoff: a whole pipeline is **declaration only**. The tech example's
prior ~120 lines of hand-written glue (`_build_prompt` / `_control_path` /
`_invoke` / `_make_run` / `_make_gate`) collapse into `agent_node(...)` calls plus
ready gates.

## Where it lives

`src/agent_flow/node_builder.py` (`agent_node`, `build_work_order`, `control_path`).
