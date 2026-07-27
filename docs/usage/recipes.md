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
| `rerun_on_signal` | `target` (required) | The agent's control verdict set `rerun_required` -> `GoTo(target)`, a **fixed** earlier node (then the flow re-flows forward). The classic "verifier re-runs its analyst". Reads the verdict from the harvested envelope (`ctx.result`) — no file, no path. |
| `rerun_on_named` | *(none)* | Same `rerun_required` signal, but routes to **whichever** node the verdict names (first valid backward target). For a coherence check that may bounce to any upstream stage. Reads the same `ctx.result` envelope. |

Signatures: `require_file(ctx, *, path, on_missing=None)`,
`rerun_on_signal(ctx, *, target)`,
`rerun_on_named(ctx)`. All three auto-populate the
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

Every consumer callable above — a gate, an `after_node` hook, an `export`, a
custom `run`, an in-process `impl` — may be a plain `def` OR an `async def`. The
engine awaits an awaitable return and offloads a blocking sync callable to a
worker thread, so you write async only where it buys you something.

## Type a node's inputs (`input_schema`)

`result_schema` types what an agent RETURNS. `input_schema` is its mirror: it
types what a node RECEIVES. The values still live in `inputs` — a schema is
their TYPE, so several nodes can share one schema with different values:

```python
class TriageIn(BaseModel):
    ticket: str
    priority: Literal["low", "normal", "high"] = "normal"

agent_node("triage-eu", "triage", input_schema=TriageIn, result_schema=TriageOut,
           inputs={"ticket": "{eu_ticket}", "priority": "high"})
agent_node("triage-us", "triage", input_schema=TriageIn, result_schema=TriageOut,
           inputs={"ticket": "{us_ticket}"})            # priority defaults
```

Validation runs on the **resolved** work order — after `{param}` templating and
upstream [`exports`](#exports) — and **before the agent is spawned**. So an
unresolved `{mode}` (a skipped upstream, a typo) fails with a real schema error
instead of reaching the agent as the literal text `{mode}`. Note this only bites
for a **constrained** field: a bare `str` accepts `"{mode}"` quite happily, so
use `Literal`, a `pattern`, or a non-`str` type where you want that guarantee.
A failure is mapped
through the node's `criticality` like any other node error: `blocking` halts the
run, `degrade` records the node as degraded and continues.

It does **not** change the prompt. Work-order lines render with the keys you
wrote, so adding a schema to an existing node cannot break the agent `.md` that
refers to `TICKET`/`REPORT`. Keep UPPERCASE keys with snake_case fields using
ordinary pydantic aliases:

```python
class TriageIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    ticket: str = Field(validation_alias="TICKET")
```

An **in-process** impl receives the validated instance as `inv.input_obj` (the
raw values stay on `inv.inputs`), which is what makes a PydanticAI node typed at
both ends — see the next recipe. A subprocess agent simply gets the same work
order it always did.

Declaratively it is a registered name, exactly like `result_schema`, so the
FlowDef stays serializable data:

```python
registry.schema("TriageIn")(TriageIn)
NodeDef(name="triage", agent="triage", inputs={...}, input_schema="TriageIn")
```

## An async in-process agent (PydanticAI)

An in-process node runs a Python callable directly — no subprocess, no control
sidecar — and maps its typed return onto the same result contract a subprocess
agent produces. Because agent-flow's engine is async-first, an async-native agent
library (PydanticAI) is a first-class node: write the `impl` as `async def` and
`await` the agent inside it. Register it by name and reference it with
`NodeDef(impl_ref=…)` (or attach it imperatively with `agent_node(impl=…)`):

```python
from agent_flow import FlowDef, NodeDef, FlowRegistry, arun_flow
from pydantic import BaseModel

class Triage(BaseModel):
    category: str
    urgent: bool

registry = FlowRegistry()
registry.schema("Triage")(Triage)

@registry.agent_impl("triage")               # the impl may be async
async def triage(inv):                        # inv = the neutral AgentInvocation
    # Typed at BOTH ends: inv.input_obj is validated from the node's inputs (see
    # input_schema above); inv.result_schema is the node's declared output type.
    #   agent = Agent(inv.model, output_type=inv.result_schema, deps_type=TriageIn)
    #   result = await agent.run(format_as_xml(inv.input_obj), deps=inv.input_obj)
    #   return result.output
    return Triage(category="bug", urgent="crash" in inv.prompt.lower())

flow = FlowDef(name="triage", nodes=[
    NodeDef(name="triage", impl_ref="triage", inputs={"TICKET": "{ticket}"}, result_schema="Triage"),
])

# On an event loop (FastAPI / notebook), await the async-native entry:
#   result = await arun_flow(flow, registry=registry, ticket="app crashes on login")
# From a plain script, the blocking run_flow wrapper does the same:
#   run_flow(flow, registry=registry, ticket="app crashes on login")
```

A gate reads the validated typed object as `ctx.obj` regardless of whether the
node ran in-process or as a subprocess — the two execution models are
interchangeable behind the node. A runnable end-to-end version (sync + async
impls mixed in one flow, both `run_flow` and `arun_flow` entry points) is
`examples/inprocess.py`.

## See what the engine is doing (logging)

agent-flow is **silent until you ask**. Importing it configures no logging and
writes nothing to stderr — a library must not hijack your output. Two ways to
turn it on:

```bash
python my_flow.py run --log-level DEBUG      # via run_cli (it configures logging for you)
```

```python
from agent_flow import setup_logging
setup_logging("DEBUG")                        # programmatic (run_flow / arun_flow)
```

`INFO` gives the run's shape (run_dir, node start/finish + duration, jump-backs,
the final summary). `DEBUG` adds the diagnostics you want when a run is stuck:
the spawned process (pid, argv, cwd), stdout EOF, stale/kill transitions, the
group walk, and which executor each node selected. Chatty third-party loggers
(asyncio, httpx, …) are pinned to WARNING so `DEBUG` stays about your flow.

Output goes to **stderr** via [loguru](https://loguru.readthedocs.io/), leaving
stdout free for the CLI's tables and event stream. Standard-library log records
(including Prefect's) are routed into the same sink, so one flag controls
everything.

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
