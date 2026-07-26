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
For the lower-level imperative form (`agent_node` / hand-built `Node` / your own
flow), see [advanced-recipes.md](advanced-recipes.md).

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

Via the CLI (adds `run` / `nodes list`, `-p/--param`, `--config`, etc.):

```python
def main():
    from agent_flow.cli import run_cli
    run_cli(flow)                 # or run_cli(flow, registry=…, params_model=…)
```

```bash
python -m my_pkg.flow run -p product_key=acme --runtime opencode
python -m my_pkg.flow nodes list          # inspect the pipeline (node -> agent, deps, gate)
```

## Require a step actually produced its file

Gates are referenced by name; config is data. The built-in `require_file`
retries the node (bounded) if the report wasn't written:

```python
NodeDef(name="tech-stack", agent="tech-stack-analyst",
        inputs={"REPORT": "{run_dir}/tech-stack.md"},
        gate="require_file", gate_args={"relpath": "tech-stack.md"})
```

## A verifier that can trigger a re-run {#a-verifier-that-can-trigger-a-re-run}

A "verifier" is just another node that depends on its subject and carries the
built-in `rerun_on_signal` gate: when the verifier's control sidecar flags a
re-run, the flow jumps back to `target` and re-flows forward.

```python
FlowDef(name="p", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst",
            inputs={"REPORT": "{run_dir}/tech-stack.md"},
            gate="require_file", gate_args={"relpath": "tech-stack.md"}),
    NodeDef(name="tech-stack-verify", agent="tech-stack-verifier",
            depends_on=["tech-stack"], criticality="degrade",
            gate="rerun_on_signal", gate_args={"target": "tech-stack"}),
])
```

`criticality="degrade"` means a failed verification records `degraded` and the
run continues; `blocking` (the default) would halt it. For a verifier that may
bounce to WHICHEVER upstream node it names, use `gate="rerun_on_named"`.

## Run independent steps in parallel

Nodes sharing a `parallel_group` fan out concurrently once their deps are met:

```python
FlowDef(name="p", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst"),
    NodeDef(name="domain",   agent="domain-analyst",   depends_on=["tech-stack"], parallel_group="analysis"),
    NodeDef(name="coupling", agent="coupling-analyst", depends_on=["tech-stack"], parallel_group="analysis"),
])
```

`domain` and `coupling` run together. Cap total concurrency with
`FlowDef(llm_concurrency=2)`.

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

## Publish a value to downstream nodes {#exports}

A node can publish part of its result into the run params so later nodes template
`{key}`. Declarative form (a field map), no code:

```python
NodeDef(name="readiness", agent="readiness-check", result_schema="ReadinessResult",
        exports={"pipeline_commit": "commit"})     # result.commit -> params["pipeline_commit"]
NodeDef(name="analyst", agent="analyst", depends_on=["readiness"],
        inputs={"COMMIT": "{pipeline_commit}"})     # available downstream
```

For arbitrary logic, register `@registry.export("name")` and set
`export_ref="name"`.

## Typed output from an agent

Register a schema by name and reference it; the agent's `result` is validated and
surfaced to gates/exports as `ctx.obj`:

```python
registry.schema("TechStack")(TechStackModel)     # a pydantic model / JSON-schema dict
NodeDef(name="tech-stack", agent="tech-stack-analyst", result_schema="TechStack")
```

See [result-schema.md](../design/orchestrator/result-schema.md) for the schema
shapes.

## Start partway, or run one node

Per-invocation entry points (CLI or `run_flow`):

```bash
python -m my_pkg.flow run --start-from tech-stack   # begin here, run forward (skip upstream)
python -m my_pkg.flow run --only tech-stack         # run ONLY this node/group, then stop
```

Both assume the skipped nodes' outputs already exist. `--start-from` and `--only`
are mutually exclusive. Names are a node or a parallel-group.

## Choose the execution backend

The default in-process backend needs nothing. Opt into Prefect for the run UI /
scale:

```python
FlowDef(name="p", backend="prefect", nodes=[...])   # or --backend prefect on the CLI
```

See [backend.md](../design/orchestrator/backend.md).

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
