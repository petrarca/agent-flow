---
type: Concept
title: FlowDef — the declarative pipeline surface
description: FlowDef/NodeDef as serializable pipeline data; the FlowRegistry (gates/exports/runs/schemas/agent_impls/mock_agents by name); compile_flow/run_flow; how it layers over the runtime Node.
tags: [agent-flow, flowdef, nodedef, registry, declarative, compile]
timestamp: 2026-07-26T00:00:00Z
---

# FlowDef — the declarative surface

`FlowDef` is the recommended way to author a pipeline: a pipeline as **pure
data**. Where `agent_node`/`Node` build the runtime graph imperatively (Python
objects that carry callables), a `FlowDef` of `NodeDef`s is a serializable
pydantic model — no callables — that **compiles** to the same runtime `Node`s.

    FlowDef / NodeDef   (data — the surface you write)
        │  compile_flow(flow_def, registry)     resolve names -> callables
        ▼
    list[Node]          (runtime — internal; carries the run/gate callables)
        │  build_flow(nodes)
        ▼
    engine execution

The engine, the runtime `Node`, `agent_node`, and `build_flow` are unchanged —
FlowDef is an additive layer whose compile target is the existing `Node`. So a
pipeline authored as a FlowDef and the same pipeline authored with `agent_node`
run identically (see `examples/declarative.py` vs `examples/imperative.py`).

## Why declarative

A `FlowDef` is data, which buys what the imperative form cannot:

- **Serializable** — `flow.to_json()` / `FlowDef.model_validate_json(...)`: store
  it, diff it between versions, transfer it, hand it to a designer/UI.
- **Authorable without Python** — the same shape can come from YAML/JSON (load
  a dict, `FlowDef.model_validate(...)`), or be generated from a template.
- **Validated before it runs** — pydantic + FlowDef validators catch unknown
  `depends_on`, duplicate names, and missing gate/schema/run references at
  definition time, not three nodes into a run.

If you need none of those, `agent_node` is a perfectly good lower-level surface;
FlowDef does not add runtime capability, only a data representation.

## NodeDef

One node, as data. Every field mirrors `agent_node`'s authoring options, but
gates/exports/schemas are **names** (resolved via the registry), never callables.

```python
NodeDef(
    name="tech-stack",
    agent="tech-stack-analyst",                 # run an agent (or run_ref=… for custom)
    inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
    depends_on=["readiness"],                    # DAG edges
    parallel_group="analysis",                   # nodes sharing a group fan out
    criticality="degrade",                       # blocking (default) | degrade
    max_cycles=1,
    gate="require_file",                         # a gate BY NAME (built-in or your own)
    gate_args={"relpath": "tech-stack.md"},      # config for the gate
    result_schema="TechStack",                   # a schema BY NAME (registry)
    exports={"stack": "detected_stack"},         # result->params map (or export_ref="name")
    impl_ref="classify",                          # OPTIONAL: run in-process (registry.agent_impl)
    instructions="…", context=["rules/*.md"],    # per-node prompt channels
    model=None, idle_timeout_s=None, agent_dir=None,
)
```

A node runs **either** an `agent` (the standard "run one agent" node) **or** a
`run_ref` (a registered custom run — see below) — exactly one. `exports` and
`export_ref` are mutually exclusive. `impl_ref` is not an alternative to `agent`:
it selects HOW the (named) agent runs — in-process rather than as a subprocess —
so it requires `agent` to be set (see "In-process & mock execution" below).

## FlowDef

The whole pipeline plus flow-wide settings.

```python
FlowDef(
    name="tech-assessment",
    nodes=[NodeDef(...), NodeDef(...), …],
    shared_instructions="…", shared_context=["{repos_root}/rules/*.md"],
    agent_dir="…",           # where the agents' .md live (opencode --dir)
    backend="inprocess",     # or "prefect"
    llm_concurrency=None,
)
```

Validation runs at construction: unique node names, every `depends_on` names a
known node. `compile_flow` additionally checks that every referenced gate /
result_schema / run_ref / export_ref / impl_ref exists in the registry.

## The FlowRegistry — names resolve to code

A FlowDef holds only names. A `FlowRegistry` holds the **implementations** they
resolve to, so the definition stays data and the code lives in one place. The
built-in gates (`require_file`, `rerun_on_signal`, `rerun_on_named`) are seeded
into every registry, so the common cases need no registration.

### Gates — `(ctx, **config) -> Directive`

A gate is a single function taking the run context plus any per-node config; the
node's `gate_args` supply the config (bound with `functools.partial` at resolve
time, so the engine still calls the gate with just `ctx`). No factory, no
returning-a-function.

```python
registry = FlowRegistry()

@registry.gate("stack_usable")               # no config — the common case
def stack_usable(ctx):
    return Stop("no stack") if ctx.result.get("status") == "error" else Continue()

@registry.gate("rerun_to")                   # configured per node via gate_args
def rerun_to(ctx, *, target):
    return GoTo(target) if _flagged(ctx) else Continue()
```

A node references a gate by name: `gate="rerun_to", gate_args={"target": "…"}`.

### Exports, custom runs, schemas

- `@registry.export("name")` — a `(payload) -> Mapping` published to run params
  for downstream nodes (`NodeDef(export_ref="name")`). The inline
  `NodeDef(exports={param: field})` map needs no registry.
- `@registry.run("name")` — a custom `(ctx) -> dict` run for a node whose work is
  NOT running an agent (`NodeDef(run_ref="name")`); the node stays serializable,
  the code lives in the registry.
- `@registry.schema("name")` — a result schema (pydantic model / JSON-schema
  dict / `ResultSchema`) referenced by `NodeDef(result_schema="name")`.

### In-process & mock execution

Two more registration kinds control HOW an agent runs, not what it does:

- `@registry.agent_impl("name")` — an **in-process** agent: a Python callable
  `(inv) -> AgentResult | pydantic model | dict` referenced by
  `NodeDef(impl_ref="name")`. The node then runs as a direct call via
  `InProcessExecutor` (no subprocess, no sidecar) instead of spawning a runtime;
  `agent` stays as the label. See [node_builder.md](node_builder.md).
- `@registry.mock_agent("name")` — a deterministic, no-token **stand-in** for the
  agent named `name`, used only under the `--mock-agents` mode
  (`mock_agents=True`). When the mode is on, any node whose `agent` matches runs
  the behaviour via `MockExecutor` instead of its normal executor; mocks are keyed
  by AGENT name, so one registration covers every node running that agent. Nodes
  without a registered mock still run for real (partial mocking). See
  [mock-agent.md](mock-agent.md).

`compile_flow` threads the registry onto each compiled agent node so a
`mock_agent` can be resolved by agent name at run time (this is why `run_flow` /
`run_cli` take the same `registry`).

### Observing hooks — cross-cutting, never steer flow

`@registry.on(event)` registers observers fired at lifecycle points:
`before_node` / `after_node` / `on_error` (per-node, optionally scoped with
`node="x"` or `node=["x","y"]`) and `before_group` / `after_group`. Hooks
observe/telemeter; only a node's gate decides `Continue/Restart/GoTo/Stop`.

```python
@registry.on("after_node")
def _log(node, outcome):
    print(f"{node.name}: {outcome.status} ({outcome.duration_s:.1f}s)")
```

## Running a FlowDef

Two entry points, no manual compile/build in the common case:

```python
from agent_flow import run_flow          # programmatic one-liner
run_flow(flow, registry=registry, product_key="acme", runtime="opencode")

from agent_flow.cli import run_cli        # the reusable CLI (run / nodes list)
run_cli(flow, registry=registry, params_model=MyParams)
```

`run_cli(flow_def)` compiles + runs it and also gives `run` and `nodes list`
subcommands. When no registry is passed, a default (built-in gates only) is used.
The FlowDef's flow-wide `agent_dir` becomes the CLI's default agent dir.

`compile_flow(flow_def, registry) -> list[Node]` and `build_flow(nodes)` remain
available for advanced use, but a normal consumer does not call them directly.

## Escape hatch

A node whose work is arbitrary Python (not "run an agent") uses `run_ref` +
`@registry.run(...)`. If you truly need a hand-built `Node` (a bespoke `run`
callable inline), the runtime `Node`/`agent_node` layer is still public — FlowDef
is the surface, not a cage.

## Where it lives

`src/agent_flow/flowdef/` — `models.py` (`FlowDef`/`NodeDef`), `compile.py`
(`compile_flow`, `run_flow`). Names resolve against
`src/agent_flow/registry.py` (`FlowRegistry`). The compile target is
`engine.Node` (see [engine.md](engine.md)); gates are documented in
[gates.md](gates.md), typed output in [result-schema.md](result-schema.md).
