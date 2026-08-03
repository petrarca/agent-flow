---
type: Guide
title: Recipes
description: Task-oriented how-tos for the FlowDef surface — define a pipeline, gates by name, parallel steps, custom logic, run it.
tags: [agent-flow, recipes, how-to, flowdef, gates, parallel]
timestamp: 2026-07-25T08:54:40Z
---

# Recipes

How-tos for the `FlowDef` surface. Assumes you have read
[getting-started.md](getting-started.md). For the imperative form, parallel
steps, typed output, exports, partial runs and backends, see
[advanced-recipes.md](advanced-recipes.md).

## Define and run a pipeline

A pipeline is a `FlowDef` of `NodeDef`s — data. Run it with `run_flow` (one call)
or hand it to the reusable CLI.

```python
from agent_flow import FlowDef, NodeDef, run_flow

flow = FlowDef(name="my-pipeline", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"}),
])

# agent_dir is run config (or omit it: the opencode runner probes for .opencode/)
run_flow(flow, product_key="acme", runtime="opencode",
         run_config={"agent_dir": "{repo}/pipelines/tech"})
```

Via the CLI (adds `run` / `flow nodes` / `version`, `-p/--param`, `--config`, etc.):

```python
def main():
    from agent_flow.cli import run_cli
    # or: run_cli(flow, registry=…, run_config={…}, version="1.2.0")
    run_cli(flow)
```

```bash
python -m my_pkg.pipeline run -p product_key=acme --runtime opencode
# inspect the pipeline: node -> agent, deps, gate
python -m my_pkg.pipeline flow nodes

# e.g. "my-pipeline 1.2.0 (agent-flow 0.1.2)"
python -m my_pkg.pipeline version
```

Pass `run_cli(flow, version="1.2.0")` with your own app/package version: `version`
then prints it as the primary version and the agent-flow version as a secondary
layer (the client/server convention). Omit it and only the agent-flow version is
shown.

## Built-in gates

A gate runs after a node's agent and returns a directive that steers the flow
(`Continue` / `Restart` / `GoTo` / `Stop`). Two are pre-seeded into every
`FlowRegistry`, so you reference them by name with `gate_args`:

| Gate | `gate_args` | What it does |
|------|-------------|--------------|
| `require_file` | `path` (required, templatable), `on_missing` (optional `Directive`) | The agent reported ok but didn't write its artifact -> `Restart` the node (bounded by `max_cycles`). `path` supports `{param}` templates (e.g. `"{run_dir}/report.md"`). A bare filename without a leading `/` or `{run_dir}` is treated as relative to `run_dir` — use `"{run_dir}/..."` to keep it consistent with the node's `inputs=` value. |
| `stop_if` | `field`, `equals` (required); `reason_field` (default `"reason"`), `label` (optional) | A precondition failed -> `Stop` the whole run. Aborts when the result's `field` equals the sentinel `equals` (e.g. `field="ready", equals="no"`), else `Continue`. Reads the field from `ctx.obj` (typed) or the raw envelope, so it works with or without a `result_schema`. `label` prefixes the Stop reason to name the stage. The deterministic readiness/precondition check. |

Signatures: `require_file(ctx, *, path, on_missing=None)` and
`stop_if(ctx, *, field, equals, reason_field="reason", label="")`. A node with
**no** gate behaves as `Continue()`. To write your own gate, see
[Hook your own logic](#hook-your-own-logic-flowregistry) below; for the full
directive/`GateContext` reference see the [gates design doc](../design/gates.md).

> **Letting the agent ask for a re-run is not a gate.** Declare the targets on
> the node instead — `rerun_targets=["tech-stack"]` — and the engine honors the
> agent's `rerun_required`, names the legal targets in its preamble, and
> validates them at build time. See [Let an agent re-run an earlier
> step](#let-an-agent-re-run-an-earlier-step) below.

## Let an agent re-run an earlier step

Declare which steps the agent may ask to re-run. That declaration is the whole
opt-in — no gate:

```python
NodeDef(name="verify", agent="tech-stack-verifier", depends_on=["tech-stack"],
        rerun_targets=["tech-stack"])                       # one target

NodeDef(name="coherence", agent="coherence-check", depends_on=["ai-leverage"],
        rerun_targets=["tech-stack", "analysis", "architecture"])   # agent picks
```

The agent's preamble gains a block naming exactly those targets, so its `.md`
never hardcodes step names. It then writes:

```json
{ "status": "verified", "rerun_required": true }
{ "status": "verified", "rerun_required": { "target": "analysis", "instruction": "tech-stack changed, redo" } }
```

`true` is allowed only when **one** target was granted (nothing to choose);
otherwise `target` is required. The `instruction` is handed to that step verbatim
on its next run.

A target may be a node or a **parallel group** — a group re-runs every member and
broadcasts the instruction to each. Targets must run *before* the declaring node;
an unknown or forward name fails at **build time**. Bounded by the target's
`max_cycles`. Full detail: [rerun.md](../design/rerun.md).

## Hook your own logic (FlowRegistry)

Write a function, register it on a `FlowRegistry`, reference it from a node by
name.

```python
from agent_flow import FlowRegistry
from agent_flow.gates import Continue, Stop

# built-in gates are already seeded
registry = FlowRegistry()

# a gate is (ctx, **config) -> Directive
@registry.gate("stack_usable")
def stack_usable(ctx):
    return Stop("no stack") if ctx.result.get("status") == "error" else Continue()

# an observing hook: telemetry only, never steers the flow
@registry.on("after_node")
def _log(node, outcome):
    print(f"{node.name}: {outcome.status} ({outcome.duration_s:.1f}s)")

flow = FlowDef(name="p", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst", gate="stack_usable"),
])
run_flow(flow, registry=registry, product_key="acme", runtime="opencode")
```

A gate configured per node just takes extra keyword params; the node's
`gate_args` supply them (`def g(ctx, *, target): …` with
`gate="g", gate_args={"target": "…"}`).

### Everything you can register

The declaration carries a name; the registry holds the implementation. That is
what keeps a `FlowDef` serializable.

| register | signature | referenced from a node by |
|---|---|---|
| `@registry.gate("name")` | `(ctx, **gate_args) -> Directive` | `gate="name"` (+ `gate_args={…}`) |
| `@registry.schema("name")` | a pydantic model, a JSON-schema dict, or a `ResultSchema` | `result_schema="name"` and `input_schema="name"` |
| `@registry.params_model("name")` | a pydantic model — the run params the flow needs | `FlowDef(params_schema="name")` |
| `@registry.export("name")` | `(payload) -> Mapping` published to downstream params | `export_ref="name"` |
| `@registry.run("name")` | `(ctx) -> dict` — a node that runs your code, not an agent | `run_ref="name"` |
| `@registry.agent_impl("name")` | `(inv) -> AgentResult / model / dict` — an in-process agent | `impl_ref="name"` |
| `@registry.mock_agent("agent")` | `(inv, ctx) -> envelope` — a token-free stand-in ([testing.md](testing.md)) | *(matched by AGENT name under `--mock-agents`)* |
| `@registry.on("event", node=…)` | an observing hook; never steers flow | *(fires automatically; `node=` scopes it)* |
| `registry.work_order(fn)` | `(resolved: dict[str, str]) -> str` — restyle the work order ([advanced](advanced-recipes.md#change-how-the-prompt-is-rendered)) | *(flow-wide; no reference)* |
| `registry.prompt(fn)` | `(parts: PromptParts) -> str` — assemble the whole prompt body ([advanced](advanced-recipes.md#change-how-the-prompt-is-rendered)) | *(flow-wide; no reference)* |

Each takes a decorator or a direct call — `@registry.schema("TriageIn")` above a
class, or `registry.schema("TriageIn")(TriageIn)` for a model defined elsewhere.
Referencing a name that is not registered fails at **compile**, before anything
runs: `node 'n': unknown input_schema 'Nope'`.

Note `schema` serves **both** ends: one registration is usable as a node's
`input_schema` and as its `result_schema`. `work_order` and `prompt` are the two
singletons — per-flow presentation choices, not something a node selects.

Working imperatively (`agent_node`) you can skip the registry entirely and pass
the callable or class directly (`gate=fn`, `input_schema=TriageIn`,
`impl=fn`); the registry exists so the DECLARATIVE surface stays serializable.

Every consumer callable above — a gate, an `after_node` hook, an `export`, a
custom `run`, an in-process `impl` — may be a plain `def` OR an `async def`. The
engine awaits an awaitable return and offloads a blocking sync callable to a
worker thread, so you write async only where it buys you something.

## Type a node's inputs (`input_schema`)

`result_schema` types what an agent RETURNS ([typed output](advanced-recipes.md#get-typed-output-from-an-agent)).
`input_schema` is its mirror: it types what a node RECEIVES. The values still
live in `inputs` — a schema is their TYPE, so several nodes can share one schema
with different values.

```python
class TriageIn(BaseModel):
    ticket: str
    priority: Literal["low", "normal", "high"] = "normal"

registry.schema("TriageIn")(TriageIn)

FlowDef(name="triage", nodes=[
    NodeDef(name="triage-eu", agent="triage", input_schema="TriageIn",
            inputs={"ticket": "{eu_ticket}", "priority": "high"}),
    NodeDef(name="triage-us", agent="triage", input_schema="TriageIn",
            inputs={"ticket": "{us_ticket}"}),          # priority defaults
])
```

Validation runs on the **resolved** work order — after `{param}` templating and
upstream [`exports`](advanced-recipes.md#exports) — and **before the agent is
spawned**. So an unresolved `{mode}` (a skipped upstream, a typo) fails with a
real schema error instead of reaching the agent as the literal text `{mode}`.
Note this only bites for a **constrained** field: a bare `str` accepts `"{mode}"`
quite happily, so use `Literal`, a `pattern`, or a non-`str` type where you want
that guarantee. A failure is mapped through the node's `criticality` like any
other node error: `blocking` halts the run, `degrade` records the node as
degraded and continues.

It does **not** change the prompt. The work order renders with the keys you
wrote, so adding a schema cannot break the agent `.md` that refers to
`TICKET`/`REPORT`. Keep UPPERCASE keys with snake_case fields using ordinary
pydantic aliases:

```python
class TriageIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    ticket: str = Field(validation_alias="TICKET")
```

An **in-process** impl receives the validated instance as `inv.input_obj` (the
raw values stay on `inv.inputs`), which is what makes a PydanticAI node typed at
both ends — see the next recipe. A subprocess agent simply gets the same work
order it always did. Imperatively, pass the class straight to
`agent_node(input_schema=TriageIn)` instead of registering a name.

## An async in-process agent (PydanticAI)

An in-process node runs a Python callable instead of a subprocess, mapping its
typed return onto the same result contract. The impl may be `async def`, so an
async agent library is a first-class node. Register it by name and reference it
with `NodeDef(impl_ref=…)`:

```python
from agent_flow import FlowDef, NodeDef, FlowRegistry, arun_flow
from pydantic import BaseModel

class Triage(BaseModel):
    category: str
    urgent: bool

registry = FlowRegistry()
registry.schema("Triage")(Triage)

# the impl may be async; inv is the neutral AgentInvocation
@registry.agent_impl("triage")
async def triage(inv):
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

A gate reads the typed object as `ctx.obj` whether the node ran in-process or as
a subprocess. Runnable version: `examples/inprocess.py`.

## Fill params just-in-time (`before_node`)

Values usually arrive at flow start or from an upstream node's `exports`. To
compute one right before a node runs, use a `before_node` hook — it fires before
the engine snapshots the run context for that node, so what you write lands in
that node's `inputs`.

```python
from agent_flow.run_context import get_run_context

# this node only
@registry.on("before_node", node="analysis")
def fill(node):
    get_run_context().update({"mode": lookup_mode(), "cutoff": today()})
```

`node=` takes a name or a list; omit it to fire for every node. Group events
(`before_group`/`after_group`) are not node-scoped and reject it.

Two constraints:

- Forward-only. The value reaches the node being started and everything after it,
  never a node that already ran or a sibling in the same parallel group.
- A hook that raises is logged and ignored. If a value is mandatory, do not
  assert in the hook — let it fail at the node's
  [`input_schema`](#type-a-nodes-inputs-input_schema).

## See what the engine is doing (logging)

Importing agent-flow configures no logging and writes nothing. Two ways to turn
it on:

```bash
# via run_cli, which configures logging for you
python my_flow.py run --log-level DEBUG
```

```python
# programmatic (run_flow / arun_flow)
from agent_flow import setup_logging

setup_logging("DEBUG")
```

`INFO` gives the run's shape (run_dir, node start/finish + duration, jump-backs,
the final summary). `DEBUG` adds the diagnostics you want when a run is stuck:
the spawned process (pid, argv, cwd), stdout EOF, stale/kill transitions, the
group walk, and which executor each node selected. Chatty third-party loggers
(asyncio, httpx, …) are pinned to WARNING so `DEBUG` stays about your flow.

Output goes to stderr, leaving stdout free for the CLI's tables and event stream.
Standard-library records (including Prefect's) route into the same sink, so one
flag controls everything.

## Run-wide brief and context

Inject a directive / rules into every agent:

```python
FlowDef(name="p",
        run_instructions="Follow the team's coding standards and cite a source for every finding.",
        run_context=["{repos_root}/rules/security.md"],
        nodes=[...])
```

Per-node equivalents are `NodeDef(instructions=…, context=[…])`.

`run_instructions` is the flow's standing brief. The CLI's `-i/--instructions`
(or run config `instructions`) is *appended* to it for one run, rather than
replacing it; `--instruct NODE=text` does the same for a single node. See
[the input plane](../design/input-plane.md).
