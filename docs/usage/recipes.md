---
type: Guide
title: Recipes
description: Task-oriented how-tos for the FlowDef surface — define a pipeline, gates by name, parallel steps, custom logic, run it.
tags: [agent-flow, recipes, how-to, flowdef, gates, parallel]
timestamp: 2026-07-25T08:54:40Z
---

# Recipes

Short, task-oriented how-tos on the **FlowDef** surface (the recommended way to
author a pipeline). Each assumes you've read [getting-started.md](getting-started.md).

> For the imperative form (`agent_node` / `build_flow` / gate callables), gates
> in depth, parallel steps, typed output, exports, start-from/only, backends, and
> live progress: see [advanced-recipes.md](advanced-recipes.md).

## Define and run a pipeline

A pipeline is a `FlowDef` of `NodeDef`s — data. Run it with `run_flow` (one call)
or hand it to the reusable CLI.

```python
from agent_flow import FlowDef, NodeDef, run_flow

flow = FlowDef(name="my-pipeline", agent_dir="{repo}/pipelines/tech", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"}),
])

run_flow(flow, product_key="acme", runtime="opencode")
```

Via the CLI (adds `run` / `flow nodes` / `version`, `-p/--param`, `--config`, etc.):

```python
def main():
    from agent_flow.cli import run_cli
    run_cli(flow)                 # or run_cli(flow, registry=…, params_model=…, version="1.2.0")
```

```bash
python -m my_pkg.pipeline run -p product_key=acme --runtime opencode
python -m my_pkg.pipeline flow nodes      # inspect the pipeline (node -> agent, deps, gate)
python -m my_pkg.pipeline version         # e.g. "my-pipeline 1.2.0 (agent-flow 0.1.2)"
```

Pass `run_cli(flow, version="1.2.0")` with your own app/package version: `version`
then prints it as the primary version and the agent-flow version as a secondary
layer (the client/server convention). Omit it and only the agent-flow version is
shown.

## Built-in gates

A **gate** runs after a node's agent and returns a directive that steers the flow
(`Continue` / `Restart` / `GoTo` / `Stop`). Three gates cover what almost every
pipeline needs; they are **pre-seeded into every `FlowRegistry`**, so you
reference them **by name** with `gate_args` — no registration, no import:

| Gate | `gate_args` | What it does |
|------|-------------|--------------|
| `require_file` | `path` (required, templatable), `on_missing` (optional `Directive`) | The agent reported ok but didn't write its artifact -> `Restart` the node (bounded by `max_cycles`). `path` supports `{param}` templates (e.g. `"{run_dir}/report.md"`). A bare filename without a leading `/` or `{run_dir}` is treated as relative to `run_dir` — use `"{run_dir}/..."` to keep it consistent with the node's `inputs=` value. |
| `rerun_on_signal` | `target` (required), `control_file` (optional) | The node's control sidecar set `rerun_required` -> `GoTo(target)`, a **fixed** earlier node (then the flow re-flows forward). The classic "verifier re-runs its analyst". `control_file` defaults to `<node>.control.json` under `run_dir` (same `run_dir` rule as `require_file` — not the process cwd). |
| `rerun_on_named` | `control_file` (optional) | Same `rerun_required` signal, but routes to **whichever** node the sidecar names (first valid backward target). For a coherence check that may bounce to any upstream stage. Same `control_file` / `run_dir` default as `rerun_on_signal`. |

Signatures: `require_file(ctx, *, path, on_missing=None)`,
`rerun_on_signal(ctx, *, target, control_file=None)`,
`rerun_on_named(ctx, *, control_file=None)`. All three auto-populate the
directive's one-time `instruction`. A node with **no** gate behaves as
`Continue()`. To write your own gate, see [Hook your own logic](#hook-your-own-logic-flowregistry)
below; for the full directive/`GateContext` reference see the
[gates design doc](../design/orchestrator/gates.md).

> The `rerun_*` gates only fire if the agent actually sets `rerun_required` in
> its control sidecar — the agent's own `.md` must be told when to set it (the
> injected control-file protocol makes the field available, but not the policy).

## Hook your own logic (FlowRegistry)

The built-in gates cover the common cases. To plug in your own logic, write a
function, register it on a `FlowRegistry`, and reference it from a node by name —
the node stays data, your code lives in the registry.

```python
from agent_flow import FlowRegistry
from agent_flow.gates import Continue, Stop

registry = FlowRegistry()                      # built-in gates already seeded

@registry.gate("stack_usable")                 # a gate is (ctx, **config) -> Directive
def stack_usable(ctx):
    return Stop("no stack") if ctx.result.get("status") == "error" else Continue()

@registry.on("after_node")                      # an observing hook (telemetry; never steers flow)
def _log(node, outcome):
    print(f"{node.name}: {outcome.status} ({outcome.duration_s:.1f}s)")

flow = FlowDef(name="p", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst", gate="stack_usable"),
])
run_flow(flow, registry=registry, product_key="acme", runtime="opencode")
```

A gate configured per node just takes extra keyword params; the node's
`gate_args` supply them (`def g(ctx, *, target): …` with
`gate="g", gate_args={"target": "…"}`). Also registrable: `@registry.export`
(a `(payload) -> params` map published downstream) and `@registry.run` (a custom
`(ctx) -> dict` node run, referenced by `NodeDef(run_ref="…")`).

## Run-wide brief and context

Inject a directive / rules into every agent:

```python
FlowDef(name="p",
        shared_instructions="Follow the team's coding standards and cite a source for every finding.",
        shared_context=["{repos_root}/rules/security.md"],
        nodes=[...])
```

Per-node equivalents are `NodeDef(instructions=…, context=[…])`. See
[the input plane](../design/orchestrator/input-plane.md).
