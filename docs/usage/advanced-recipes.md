---
type: Guide
title: Advanced recipes (lower-level)
description: Lower-level how-tos using the imperative agent_node/build_flow surface and gate callables — gates, re-run loops, parallel steps, typed output, live events, dropping to a lower tier.
tags: [agent-flow, advanced, recipes, how-to, agent_node, gates, cli]
timestamp: 2026-07-23T08:54:40Z
---

# Advanced recipes (lower-level)

These how-tos use the **imperative** surface — `agent_node` / `build_flow` and
gate callables — which sits below the declarative [FlowDef](../design/orchestrator/flowdef.md).
For the recommended FlowDef recipes, see [recipes.md](recipes.md); this page is
for when you build runtime nodes directly, write your own flow, or need the
lower-level detail. Each assumes you've read [getting-started.md](getting-started.md).

## Run a pipeline from the CLI (no bespoke command)

Instead of hand-writing a Typer command, hand your node list to the library's
reusable runner. Your whole `main` becomes:

```python
def main() -> None:
    from agent_flow import run_cli
    run_cli(build_nodes, name="my-pipeline", default_agent_dir=_PACKAGE_DIR)
```

`run_cli` provides a unified CLI:

```bash
# generic settings as flags + DOMAIN params via -p/--param KEY=VALUE:
python -m my_pkg.pipeline run -p product_key=my-product -p repos_root=/tmp/repos \
    --runtime opencode --run-dir "{repos_root}/{product_key}/output" -i "cite a source for every finding"

# or put the generic settings in a YAML config file:
python -m my_pkg.pipeline run --config run.yml -p product_key=my-product
```

```yaml
# run.yml — generic run settings (the lowest explicit source)
runtime: opencode
run_dir: "{repos_root}/{product_key}/output"
agent_dir: /work/pipelines/tech-assessment
llm_concurrency: 2
instructions: |
  Follow the team's coding standards and cite a source for every finding.
node_instructions:            # per-node steering, appended last to that node
  analyst: "Weight the security assessment heavily."
```

**Generic settings** resolve via `RunConfig` (a pydantic-settings model) with
precedence **CLI flag > env `AGENT_FLOW_*` > `.env` > `--config` YAML > default**.

**Domain params** are passed to `pipeline(**params)` and are usable as `{name}`
templates in `inputs`, `context`, and paths — so `-p product_key=my-product`
makes `{product_key}` resolve everywhere. There is no `--product` option built
in; `--param` is the generic protocol for all of them.

### Typed, required domain params (`params_model`)

Pass a pydantic-settings class to declare which params are required and validate
their types before any agent runs:

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class MyParams(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    product_key: str                        # required — no default
    repos_root: str = Field(default="/tmp/repos")

run_cli(build_nodes, name="my-pipeline", params_model=MyParams)
```

Domain params resolve **`-p` > bare env / `.env` > model default** and are
validated: a missing required param (or a bad value) fails fast with exit code 2
**before** any agent is spawned. Omit `params_model` to accept raw untyped `-p`
values (the historical behavior).

**Runtime-populated fields.** Some params are not user inputs — they are filled
*at run time* (e.g. a value a node publishes via [`exports`](#exports)). Declare
such a field with a placeholder default and mark it with the `runtime_param()`
helper (so you don't hand-encode the marker):

```python
from agent_flow import runtime_param

class MyParams(BaseSettings):
    product_key: str                        # user input
    # set at run time by a node's exports; not something you pass with -p:
    analysis_timestamp: str = Field(default="UNKNOWN", json_schema_extra=runtime_param())
```

The placeholder keeps `{analysis_timestamp}` templating resolvable from the very
first node, while `run_cli` **omits runtime fields from the "Resolved parameters"
summary** so they don't read as inputs you could pass. A node then overwrites the
placeholder for downstream nodes (see [`exports`](#exports)).

Prefer your own CLI? `run_cli` is optional — build any CLI you like and call
`build_flow(...)` / `pipeline(**params)` directly (see the toy example, which
reuses `build_run_config` and `preflight` in its own Tier-2 CLI).

## Check that a step actually produced its file

```python
agent_node("hello", "hello-analyst", inputs={"REPORT": "{run_dir}/hello.md"},
           gate_ref="require_file", gate_args={"relpath": "hello.md"})
```

Built-in gates are referenced **by name** (`gate_ref="require_file"` +
`gate_args={...}`); the `gate=` param takes only a bare callable you wrote. If the
control sidecar says `ok` but `hello.md` is missing or empty, the node is re-run
(bounded by `max_cycles`, default 1). See
[gates.md](../design/orchestrator/gates.md).

## A verifier that can trigger a re-run

There's no built-in "verifier" concept — it's a plain node that depends on the
step it checks, with a gate that can send the flow back:

```python
nodes = [
    agent_node("hello", "hello-analyst", inputs={"REPORT": "{run_dir}/hello.md"},
               gate_ref="require_file", gate_args={"relpath": "hello.md"}),
    agent_node("hello-verify", "hello-verifier",
               depends_on=("hello",),
               inputs={"REPORT": "{run_dir}/hello.md"},
               criticality="degrade",           # a failed check shouldn't halt the run
               gate_ref="rerun_on_signal", gate_args={"target": "hello"}),
]
```

`hello-verifier.md` must itself say **when** to set `rerun_required` — see
[writing-agents.md](writing-agents.md#using-rerun_required-optional--most-agents-never-need-this).
The re-run is bounded by `hello`'s `max_cycles` (default 1) — it will not loop
forever even if the verifier keeps asking.

## Run independent steps in parallel

Nodes sharing a `parallel_group` are dispatched concurrently once their
dependencies are met:

```python
nodes = [
    agent_node("tech-stack", "tech-stack-analyst", inputs={"REPORT": "{run_dir}/tech-stack.md"}),
    agent_node("domain", "domain-analyst", depends_on=("tech-stack",),
               parallel_group="analysis", inputs={"REPORT": "{run_dir}/domain.md"}),
    agent_node("coupling", "coupling-analyst", depends_on=("tech-stack",),
               parallel_group="analysis", inputs={"REPORT": "{run_dir}/coupling.md"}),
]
```

`domain` and `coupling` both depend only on `tech-stack`, so they run
concurrently. Cap total concurrency with `build_flow(..., llm_concurrency=2)`.

## Pass a run-wide brief to every agent

A global directive every agent should see — e.g. from your CLI:

```python
build_flow(nodes, name="my-pipeline", shared_instructions=brief)
```

```bash
python my_flow.py run --instructions "Follow the team's coding standards and cite a source for every finding."
```

(`--instructions`/`-i` is already wired by `run_cli` — no need to add it
yourself; see `examples/declarative.py`.) It's injected into **every** agent's
prompt, after the control protocol. See
[input-plane.md](../design/orchestrator/input-plane.md).

## Give one node an extra, specific instruction

Additive to the run-wide brief, for one node only (set at BUILD time):

```python
agent_node("tech-stack", "tech-stack-analyst",
           inputs={"REPORT": "{run_dir}/tech-stack.md"},
           instructions="List concrete versions where known; prefer a compact table.")
```

## Steer one node at RUN time (`--instruct` / `node_instructions`)

To steer a node for a run *without* editing `build_nodes()` — or to persist
per-product steering in config — attach a per-node instruction at run time. It is
appended **LAST** (after the build-time instruction, before the work order), so it
is the most recent standing guidance and overrides earlier ones by recency:

```bash
# CLI (repeatable; NODE=text like -p):
python my_flow.py run -p product_key=acme \
  --instruct analyst="Ignore the compact-table instruction; produce the full breakdown." \
  --instruct summary="Lead with the tenancy gap."
```

```yaml
# --config run.yml — persist it per product (parallel to params:)
node_instructions:
  analyst: "Weight the security assessment heavily for Dim 14."
```

```python
# programmatic
build_flow(nodes, name="my-pipeline")(product_key="acme",
    node_instructions={"analyst": "…", "summary": "…"})
```

CLI `--instruct` merges over the config `node_instructions:` (CLI wins per node).
Pairs naturally with re-entering the flow at a node to iterate. See
[input-plane](../design/orchestrator/input-plane.md#per-node-run-time-instructions).

## Start partway through the flow (`--start-from`)

Begin at a chosen node (or parallel-group) and run forward, skipping the nodes
before it — to iterate on a late stage without re-running the expensive upstream:

```bash
# re-run only extractor -> summary -> …, steering the extractor for this pass:
python my_flow.py run -p product_key=acme \
  --start-from extractor \
  --instruct extractor="re-derive the counts; the na bucket looked off"
```

```python
build_flow(nodes, name="my-pipeline")(product_key="acme", start_from="extractor")
```

`start_from` names a **node** or a **parallel-group** (`agent_node(parallel_group=…)`):
a group name enters the whole fan-out; a member node resolves to the same group
(you can't enter "in the middle" of a parallel group — the group is the unit).

**Caveat — you assert the upstream is done.** Skipping earlier nodes means their
side-effects did not happen this run: their **output files must already exist**
(from a prior run) for the start node to read, and any params they would have
**exported** (e.g. a readiness check's `pipeline_commit`) won't be set — runtime
-populated params fall back to their defaults. `start_from` is a forward entry
point set once; it is distinct from re-run jump-back (a gate can still send the
flow back to a skipped node, and it will run then). CLI/programmatic only — not a
persisted config setting.

## Run a single node and stop (`--only`)

The surgical complement to `--start-from`: run **exactly one** node (or
parallel-group) and stop — for iterating on one stage without running anything
after it either. `--start-from` runs *from* a group *to the end*; `--only` runs
*just* that group.

```bash
# re-run ONLY the extractor, nothing before or after it:
python my_flow.py run -p product_key=acme \
  --only extractor \
  --instruct extractor="re-derive the counts; the na bucket looked off"
```

```python
build_flow(nodes, name="my-pipeline")(product_key="acme", only="extractor")
```

Same **group granularity** as `start_from`: a group name runs the whole fan-out;
a member node resolves to its group (you can't run half a parallel group). In
`--only` mode the walk runs that one group and stops — **gate jump-backs are
ignored** (there is nothing downstream to resume into).

The same upstream caveat applies, and now downstream too: everything else is
skipped, so any output files or exported params the chosen node depends on must
already exist (runtime-populated params fall back to their defaults). `--only`
and `--start-from` are **mutually exclusive** — setting both is an error.
CLI/programmatic only — not a persisted config setting.

## Make agents actually read rules/standards (ingest context)

Telling an agent to "read the security rules" is unreliable; injecting the
rules' **content** into the prompt is not. Name context files — globally and/or
per node — and the engine reads them and puts their content in the prompt:

```python
# every agent gets the security + style rules:
build_flow(nodes, name="p",
           shared_context=["{run_dir}/rules/security.md", "{run_dir}/rules/coding-standards.md"])

# only the architecture node also gets the architecture rules:
agent_node("architecture", "architecture-analyst",
           inputs={"REPORT": "{run_dir}/architecture.md"},
           context=["{run_dir}/rules/architecture.md"])
```

Sources are paths or globs, may template run params (`{run_dir}`, `{product_key}`,
…), and a source matching no file is warned about and skipped (never a crash).
Content is injected *before* the inline instructions at each scope. See
[input-plane.md](../design/orchestrator/input-plane.md). (At Tier 1/2, read the
files yourself — or use `agent_flow.read_context_blocks(...)` — and pass the
resulting string as `run_agent(shared_context=...)`.)

## Get typed output from an agent

```python
# No Pydantic needed — a plain JSON-schema dict:
schema = {"type": "object", "properties": {"summary": {"type": "string"}}, "required": ["summary"]}
agent_node("tech-stack", "tech-stack-analyst", result_schema=schema)

# Or, with a Pydantic model (pydantic is a core dependency — always available):
from pydantic import BaseModel
from agent_flow import PydanticSchema

class TechStackResult(BaseModel):
    summary: str
    languages: list[str]

agent_node("tech-stack", "tech-stack-analyst", result_schema=PydanticSchema(TechStackResult))
```

The schema is injected into the agent's prompt and the result is validated
(never fails the run — check `result["_result_valid"]` in a gate if you need to
act on it). See [result-schema.md](../design/orchestrator/result-schema.md).

## Publish a value to downstream nodes (`exports`) {#exports}

A node can discover a value and hand it to the nodes that follow — without
threading it through your own code. `agent_node(exports=...)` merges values from
the node's `result` into the run-scoped param store, so **downstream** nodes
template `{name}` against them.

```python
# Declarative: copy result fields into params (rename allowed via the key).
agent_node("readiness", "readiness-check", result_schema=ReadinessResult,
           exports={"analysis_timestamp": "analysis_timestamp",
                    "pipeline_commit": "pipeline_commit"})

# Callable: full control. With a result_schema set, exports receives the
# VALIDATED typed object directly (else the raw dict) — no key digging.
agent_node("readiness", "readiness-check", result_schema=ReadinessResult,
           exports=lambda r: {"mode": r.suggested_mode})

# A later node then templates the exported value like any other param:
agent_node("analyst", "analyst", depends_on=("readiness",),
           inputs={"ANALYSIS_TIMESTAMP": "{analysis_timestamp}", "MODE": "{mode}"})
```

The engine applies exports after the node settles, so later nodes pick the values
up automatically. Pair this with a **runtime-populated** params field (see
[typed params](#typed-required-domain-params-params_model)) to give the
placeholder a sensible default and keep it out of the resolved-params summary.

Scope: same-process, **downstream-only** — exports reach nodes that run *after*
the publisher, never parallel-group siblings. See
[input-plane.md](../design/orchestrator/input-plane.md#run-context-params-can-also-flow-from-a-node).

### What a gate sees

Prefer the typed object; fall back to the dict:

- **`ctx.obj`** — the **validated pydantic instance** when the node attached a
  `PydanticSchema`, else `None`. Read structured fields directly: `ctx.obj.ready`.
- **`ctx.result`** — the raw envelope. The agent's result dict is at
  `ctx.result["result"]`; schema-check flags at `ctx.result["_result_valid"]` /
  `["_result_errors"]`. Use this for the no-schema case or the envelope fields.

```python
def gate(ctx):
    if ctx.obj is not None:                              # Pydantic: typed fields
        return Restart() if len(ctx.obj.languages) < 2 else Continue()

    if not ctx.result["_result_valid"]:                 # schema check failed
        return Restart(instruction=f"invalid result: {ctx.result['_result_errors']}")

    data = ctx.result.get("result", {})                 # dict/no-schema case
    return Restart() if not data.get("summary") else Continue()
```

In short: **read `ctx.obj` when you set a schema; `ctx.result` otherwise.**

## Choose the execution backend (`--backend`)

The DAG runs on a swappable execution backend. The default is a Prefect-free
**local** backend — in-process threads for parallel groups, a semaphore for the
concurrency limit, stdlib logging. No temporary server, fast startup, one fewer
heavy dependency. It is the right choice for everyday single runs.

```bash
# default: in-process backend (nothing to pass)
python my_flow.py run -p product_key=acme

# opt into Prefect for the run UI / history / scheduling / scale
python my_flow.py run -p product_key=acme --backend prefect
# or persist the choice for a session:
export AGENT_FLOW_BACKEND=prefect
```

```python
build_flow(nodes, name="p", backend="inprocess")     # default
build_flow(nodes, name="p", backend="prefect")   # opt-in
```

The **engine owns the flow logic** (ordering, parallel fan-out, jump-back,
`--start-from`/`--only`, run-context); the backend only executes. So switching
backends changes *how* nodes run, never *what* runs or in what order — the
outcomes are identical. Prefect is imported only when you select `prefect`, so
the core primitives and a default local run stay Prefect-free.

Adding another backend (Hatchet, Temporal, a bespoke loop) = subclass
`FlowBackend` and register it; nothing in your pipeline changes.

## Watch progress live

Via `run_cli`, the **default** view prints one line per node transition, with the
agent label and elapsed time, then an end-of-run table:

```
    > tech-stack running (tech-stack-analyst)
check tech-stack ok (tech-stack-analyst) 15.2s
    > domain running (domain-analyst)
...
┏━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┓
┃ Node        ┃ Agent                       ┃ Outcome ┃ Duration ┃
┡━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━┩
│ tech-stack  │ opencode:tech-stack-analyst │ ok      │    15.2s │
```

The Agent column is **runtime-qualified**: `<runtime>:<agent>` — the runtime
being how the node actually ran (`opencode` / `claude` for a subprocess runtime,
`inproc` for an in-process impl, `mock` for a `--mock-agents` substitution). With
partial mocking, real and mocked nodes are told apart at a glance
(`opencode:analyst` vs `mock:verifier`).

Pass `--show-events/-v` instead for the raw per-event firehose (one projected
line per agent event — `tool edit … (+12/-3)`, text, `step done (N tokens)`) —
useful for debugging. **Ctrl-C** stops cleanly: the running agent's process group
is killed and the CLI exits 130 (no orphaned opencode, no raw traceback).

`--show-diffs` renders each edit/write as a diff block. It **composes** with the
other views — use it alone to keep the compact node table and *also* see what each
edit changed, or with `--show-events` to add diff blocks to the firehose:

| flags | base view | diff blocks |
|---|---|---|
| (none) | node table | no |
| `--show-diffs` | node table | yes |
| `--show-events` | firehose | no |
| `--show-events --show-diffs` | firehose | yes |

Each diff block is bracketed by a thin hairline rule — the top rule labelled with
the file — so it stands out from surrounding log lines. Pick the layout with
`--diff-style` (or `AGENT_FLOW_DIFF_STYLE`):

- `unified` (default) — one column: magenta hunk header, red `-` removals / green
  `+` additions, dim context. The diff header noise (`Index:`/`---`/`+++`) is
  stripped since the tool line already names the file. Robust on any width.
- `split` — side-by-side two columns (old | new), for wide terminals / large edits.

```bash
… --show-diffs                 # unified (default)
… --show-diffs --diff-style split
```

It is line-based on purpose (no repainting TUI), so it interleaves cleanly with
logs and works in non-TTY/CI. To build your own view (a Live table, a TUI),
subscribe to the same hooks directly:

```python
from agent_flow.cli import event_printer, get_console

console = get_console()
pipeline = build_flow(
    nodes,
    name="my-pipeline",
    on_node_event=my_progress.on_node_event,  # (node, phase, status, agent)
    on_event_factory=lambda agent: event_printer(agent, console=console),
)
```

The flow returns `dict[str, NodeOutcome]` (status + `duration_s` per node). See
[cli-events.md](../design/orchestrator/cli-events.md).

## Drop to a lower tier for full control

`agent_node` covers the common "one agent, KEY: value inputs" case. When it
doesn't fit — e.g. one node needs to call two agents in sequence, or compose a
prompt in a way `inputs`/`instructions` can't express — write the `Node`'s `run`
yourself:

```python
from agent_flow import Node, run_agent, get_runner

def run(ctx):
    r = run_agent(agent="my-agent", prompt="...", run_dir=ctx.run_dir,
                   runner=get_runner(ctx.params.get("runtime", "opencode")),
                   control_file=ctx.run_dir / "my-agent.control.json")
    return r.control

Node("custom", run=run, depends_on=("hello",))
```

This still plugs into `build_flow` — a hand-written `Node` and an `agent_node`
mix freely in the same graph. Or skip the declarative engine entirely and call
`run_agent` inside your own Prefect flow (Tier 2 — see `examples/custom_flow.py`),
or outside Prefect altogether (Tier 1). See
[index.md](../design/orchestrator/index.md) for the three tiers.
