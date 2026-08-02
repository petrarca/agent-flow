---
type: Guide
title: Advanced recipes
description: Advanced how-tos — parallel steps, re-run loops, exports, typed output, partial runs, prompt rendering, backends, live events, and dropping below the declarative surface.
tags: [agent-flow, advanced, recipes, how-to, agent_node, gates, cli]
timestamp: 2026-07-23T08:54:40Z
---

# Advanced recipes

Beyond the basics in [recipes.md](recipes.md): parallel steps, re-run loops,
exports, partial runs, prompt rendering and backends. Examples use `FlowDef`,
the recommended surface; the last two sections drop below it deliberately.

Where a snippet calls a `build_flow(...)` pipeline directly, note it returns an
*async* callable — `await` it or use `anyio.run(lambda: pipeline(...))`.
`run_flow` and `run_cli` bridge it for you.

## Run a pipeline from the CLI (no bespoke command)

Instead of hand-writing a Typer command, hand your node list to the library's
reusable runner. Your whole `main` becomes:

```python
def main() -> None:
    from agent_flow import run_cli
    # run_config= is the pipeline's own run-config defaults (the lowest explicit
    # source). agent_dir may be omitted entirely if you run from a dir whose
    # cwd/ancestors contain a .opencode/ — the runner probes for it.
    run_cli(build_nodes, name="my-pipeline", run_config={"agent_dir": _PACKAGE_DIR})
```

`run_cli` provides a unified CLI:

```bash
# generic settings as flags + DOMAIN params via -p/--param KEY=VALUE:
python -m my_pkg.pipeline run -p product_key=my-product -p repos_root=/tmp/repos \
    --runtime opencode --run-dir "{repos_root}/{product_key}/output" -i "cite a source for every finding"

# or put the generic settings in a config file (--config is repeatable and also
# accepts inline JSON; later --config values deep-merge over earlier ones):
python -m my_pkg.pipeline run --config run.yml -p product_key=my-product
python -m my_pkg.pipeline run --config run.yml --config '{"durations": {"long": 900}}' -p product_key=my-product
```

```yaml
# run.yml — generic run settings (the lowest explicit source)
runtime: opencode
run_dir: "{repos_root}/{product_key}/output"
# often unnecessary: the runner probes for .opencode/
agent_dir: /work/pipelines/tech-assessment
llm_concurrency: 2
instructions: |
  Follow the team's coding standards and cite a source for every finding.
# map a node's portable duration NAME to seconds
durations: {long: 900}

# runtime-specific settings, an open bag
options: {serve_url: "http://localhost:4096"}
nodes:                          # per-node run config (the shadow of a NodeDef)
  analyst:
    # appended last to that node's prompt
    instructions: "Weight the security assessment heavily."
    duration: long
    model: azure-claude/Claude-Opus-5
```

**Generic settings** resolve via `RunConfig` (a pydantic-settings model) with
precedence **CLI flag > env `AGENT_FLOW_*` > `.env` > `--config` YAML > default**.

**Domain params** are passed to `pipeline(**params)` and are usable as `{name}`
templates in `inputs`, `context`, and paths — so `-p product_key=my-product`
makes `{product_key}` resolve everywhere. There is no `--product` option built
in; `--param` is the generic protocol for all of them.

### Typed, required domain params

Declare which params the pipeline requires and validate their types before any
agent runs. The model is the flow's SIGNATURE — what it needs to RUN (as opposed
to `run_config`, which is how/where it runs).

```python
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# register it BY NAME so a FlowDef can reference it
@registry.params_model("MyParams")
class MyParams(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    product_key: str                          # required
    repos_root: str = Field(default="/tmp/repos")

# DECLARATIVE (preferred): the flow declares its own signature.
FLOW = FlowDef(name="my-pipeline", params_schema="MyParams", nodes=[...])
run_cli(FLOW, registry=registry)

# IMPERATIVE: the build_nodes form has no FlowDef to declare on, so pass it here.
run_cli(build_nodes, name="my-pipeline", params_model=MyParams)
```

Prefer the declarative form: the pairing "flow ↔ its params" then travels WITH
the flow, so two flows in one app cannot be started with each other's params, and
a serialized FlowDef says what it needs. An explicit `params_model=` still wins
over a flow's `params_schema` (it is also the override hatch).

A plain `BaseModel` works too — use `BaseSettings` only when you want bare-env /
`.env` fallback and `validation_alias` lookups.

Domain params resolve **`-p` > bare env / `.env` > model default** and are
validated: a missing required param (or a bad value) fails fast with exit code 2
**before** any agent is spawned. Declare neither to accept raw untyped `-p`
values (the historical behavior).

**Runtime-populated fields.** Some params are not user inputs — they are filled
*at run time* (e.g. a value a node publishes via [`exports`](#exports)). Declare
such a field with a placeholder default and mark it with the `runtime_param()`
helper (so you don't hand-encode the marker):

```python
from agent_flow import runtime_param

class MyParams(BaseSettings):
    product_key: str                          # user input
    # set at run time by a node's exports; not something you pass with -p:
    analysis_timestamp: str = Field(default="UNKNOWN", json_schema_extra=runtime_param())
```

The placeholder keeps `{analysis_timestamp}` templating resolvable from the very
first node, while `run_cli` **omits runtime fields from the "Resolved parameters"
summary** so they don't read as inputs you could pass. A node then overwrites the
placeholder for downstream nodes (see [`exports`](#exports)).

### Your own CLI, or other tooling

`run_cli` is optional. To drive a flow from your own CLI, a web service or any
other tooling, resolve the settings yourself with `build_run_config(...)` and
hand the result over:

```python
from agent_flow import build_run_config, run_flow

cfg = build_run_config(
    config_file=args.config,        # --config: path or inline JSON, repeatable
    model=args.model,               # your CLI's flags — highest precedence
    show_diffs=args.show_diffs,
)

run_flow(flow, run_config=cfg, product_key=args.product_key)
print(cfg.show_diffs)               # the same object drives your own display
```

`build_run_config` applies the full source stack once — your flags beat
`AGENT_FLOW_*` env, which beats `.env`, which beats `--config`. Passing the
resulting **`RunConfig`** to `run_config=` is then honoured verbatim: it is
already resolved, so it is not re-layered underneath env. (A plain **dict** means
"my defaults" and stays the lowest layer, which is what a pipeline author wants
in `run_cli(run_config={...})`.)

There is no settings singleton to install — each run holds its own config, so two
flows in one process never share or clobber each other's settings.

## Check that a step actually produced its file

```python
NodeDef(name="hello", agent="hello-analyst",
        inputs={"REPORT": "{run_dir}/hello.md"},
        gate="require_file", gate_args={"path": "{run_dir}/hello.md"})
```

If the sidecar says `ok` but the file is missing or empty, the node re-runs,
bounded by `max_cycles` (default 1). Imperatively the same gate is
`agent_node(..., gate_ref="require_file", gate_args={...})`; `gate=` there takes
a bare callable you wrote.

## A verifier that can trigger a re-run

A verifier is a plain node that depends on the step it checks, with a gate that
can send the flow back:

```python
FlowDef(name="p", nodes=[
    NodeDef(name="hello", agent="hello-analyst",
            inputs={"REPORT": "{run_dir}/hello.md"},
            gate="require_file", gate_args={"path": "{run_dir}/hello.md"}),
    NodeDef(name="hello-verify", agent="hello-verifier",
            depends_on=["hello"],
            inputs={"REPORT": "{run_dir}/hello.md"},
            # a failed check shouldn't halt the run
            criticality="degrade",
            gate="rerun_on_signal", gate_args={"target": "hello"}),
])
```

The agent's own `.md` must say when to set `rerun_required` — see
[writing-agents.md](writing-agents.md#requesting-a-re-run). The re-run is bounded
by `hello`'s `max_cycles`, so it cannot loop forever.

## Run independent steps in parallel

Nodes sharing a `parallel_group` are dispatched concurrently once their
dependencies are met:

```python
FlowDef(name="p", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst",
            inputs={"REPORT": "{run_dir}/tech-stack.md"}),
    NodeDef(name="domain", agent="domain-analyst", depends_on=["tech-stack"],
            parallel_group="analysis", inputs={"REPORT": "{run_dir}/domain.md"}),
    NodeDef(name="coupling", agent="coupling-analyst", depends_on=["tech-stack"],
            parallel_group="analysis", inputs={"REPORT": "{run_dir}/coupling.md"}),
])
```

`domain` and `coupling` both depend only on `tech-stack`, so they run
concurrently. Cap total concurrency with `run_config={"llm_concurrency": 2}`.

## Pass a run-wide brief to every agent

A directive every agent should see:

```python
FlowDef(name="my-pipeline", run_instructions=brief, nodes=[...])
```

```bash
python my_flow.py run --instructions "Follow the team's coding standards and cite a source for every finding."
```

`run_cli` wires `-i/--instructions` for you. The text is appended to the flow's
own `run_instructions` (it does not replace it) and injected into every prompt
after the control protocol. See
[input-plane.md](../design/input-plane.md).

## Give one node an extra, specific instruction

Additive to the run-wide brief, for one node only (set at BUILD time):

```python
NodeDef(name="tech-stack", agent="tech-stack-analyst",
        inputs={"REPORT": "{run_dir}/tech-stack.md"},
        instructions="List concrete versions where known; prefer a compact table.")
```

## Steer one node at RUN time (`--instruct` / `nodes.<n>.instructions`)

Attach a per-node instruction at run time, without editing the flow. It is
appended after the build-time instruction and before the work order, so it is the
last standing guidance the agent reads:

```bash
# CLI (repeatable; NODE=text like -p):
python my_flow.py run -p product_key=acme \
  --instruct analyst="Ignore the compact-table instruction; produce the full breakdown." \
  --instruct summary="Lead with the tenancy gap."
```

```yaml
# --config run.yml — persist it per product (parallel to params:)
nodes:
  analyst: {instructions: "Weight the security assessment heavily for Dim 14."}
```

```python
# programmatic — run-time per-node steering lives in run_config= (the nodes: map)
run_flow(flow_def, run_config={"nodes": {"analyst": {"instructions": "…"}, "summary": {"instructions": "…"}}})
```

`--instruct` wins over a config `nodes:` entry, per node. An unknown node name
is a hard error. Pairs well with re-entering the flow at a node to iterate.

## Start partway through the flow (`--start-from`)

Begin at a node (or parallel-group) and run forward, to iterate on a late stage
without re-running the expensive upstream:

```bash
# re-run only extractor -> summary -> …, steering the extractor for this pass:
python my_flow.py run -p product_key=acme \
  --start-from extractor \
  --instruct extractor="re-derive the counts; the na bucket looked off"
```

```python
run_flow(flow, product_key="acme", start_from="extractor")
```

A group name enters the whole fan-out; a member node resolves to its group. You
cannot enter the middle of a parallel group.

You are asserting the upstream already ran. Skipped nodes produce no files and
export no params this run, so the start node's inputs must already exist and
runtime-populated params fall back to their defaults. A gate can still jump back
into a skipped node, and it will run then. CLI and programmatic only, never
persisted in config.

## Stop after a node (`--stop-after`)

The upper-bound complement of `--start-from`: run **up to and including** a node
(or parallel-group), then stop — the named node is the last one executed.

```bash
# run the flow only as far as the extractor, then stop:
python my_flow.py run -p product_key=acme --stop-after extractor
```

```python
run_flow(flow, product_key="acme", stop_after="extractor")
```

Combine the two to run an arbitrary **segment** — everything from `A` through `B`,
both inclusive:

```bash
# run the analyst -> verify -> extractor slice, nothing before or after:
python my_flow.py run -p product_key=acme --start-from analyst --stop-after extractor
```

Same group granularity as `--start-from` (a group is indivisible: if the stop
node is in a fan-out, the whole group is the last thing that runs). Jump-backs
still work *within* the range — a verifier inside it can bounce to an analyst
inside it and re-flow forward, bounded by the stop. A `--stop-after` that lands
*before* `--start-from` is an empty range and errors. `--stop-after` is exclusive
with `--only` (which is already a single group).

## Run a single node and stop (`--only`)

`--start-from` runs from a group to the end, `--start-from` + `--stop-after` runs
a segment, and `--only` runs just that one group and stops.

```bash
# re-run ONLY the extractor, nothing before or after it:
python my_flow.py run -p product_key=acme \
  --only extractor \
  --instruct extractor="re-derive the counts; the na bucket looked off"
```

```python
run_flow(flow, product_key="acme", only="extractor")
```

Same group granularity as `start_from`. Gate jump-backs are ignored in `--only`
mode, since there is nothing downstream to resume into. The upstream caveat above
applies in both directions: everything else is skipped, so whatever the node
reads must already exist. `--only` is mutually exclusive with `--start-from` and
`--stop-after` (use those two together for a range).

## Change how the prompt is rendered

A node's `inputs` render into the prompt as XML tags by default:

```
<PRODUCT_KEY>acme</PRODUCT_KEY>
<REPORT>/run/report.md</REPORT>
```

A closing tag delimits the value, so a multi-line value is unambiguous — a
line-oriented `KEY: value` order has no continuation marker. Your agent `.md` is
unaffected either way: it refers to the key *name*.

To override it, register a renderer:

```python
from agent_flow import render_work_order_lines

# opt back into the KEY: value shape
registry.work_order(render_work_order_lines)
```

### Take over the whole prompt body

`work_order` restyles the data block. To control the entire layout, register a
prompt renderer instead. It receives the channels unassembled:

```python
# the default renderer, if you want to fall back to it
from agent_flow import render_prompt

@registry.prompt
def all_xml(parts):
    out = []
    for tag in ("run_context", "run_instructions", "run_additional_instructions",
                "node_context", "node_instructions", "node_runtime_instructions",
                "attempt_instruction"):
        if (val := getattr(parts, tag)):
            out.append(f"<{tag}>\n{val}\n</{tag}>")
    out.append(f"<work_order>\n{parts.work_order}\n</work_order>")
    return "\n".join(out)
```

The two compose: the work-order renderer produces `parts.work_order`, then the
prompt renderer lays out the body. `parts.inputs` keeps the data structured, so a
prompt renderer can format it itself.

The completion protocol is the one block you cannot render — it is owned by the
runner and prepended after your renderer runs. To change it, implement
`build_verdict_preamble` on a runner. See
[input-plane](../design/input-plane.md).

Or supply your own — any `(resolved: dict[str, str]) -> str`:

```python
@registry.work_order
def as_json(resolved):
    import json
    return json.dumps(resolved, indent=2)
```

It receives the **resolved** work order (after templating and upstream exports),
so a renderer never has to think about `{param}` substitution.

## Make agents actually read rules/standards (ingest context)

Telling an agent to read the security rules is unreliable; injecting their
content is not. Name context files, run-wide or per node, and the engine reads
them into the prompt:

```python
FlowDef(
    name="p",
    # every agent gets the security + style rules
    run_context=["{run_dir}/rules/security.md", "{run_dir}/rules/coding-standards.md"],
    nodes=[
        # only this node also gets the architecture rules
        NodeDef(name="architecture", agent="architecture-analyst",
                inputs={"REPORT": "{run_dir}/architecture.md"},
                context=["{run_dir}/rules/architecture.md"]),
    ],
)
```

Sources are paths or globs and may template run params. A source matching no
file is warned about and skipped, never a crash. Content is injected before the
inline instructions at each scope. Calling `run_agent` directly, pass an
already-read string as `run_context=` (`read_context_blocks` does the reading).

## Get typed output from an agent

Register the schema by name, then reference it. A pydantic model or a plain
JSON-schema dict both work:

```python
from pydantic import BaseModel

class TechStackResult(BaseModel):
    summary: str
    languages: list[str]

class TechStackIn(BaseModel):
    product_key: str
    report: str

registry.schema("TechStackResult")(TechStackResult)
registry.schema("TechStackIn")(TechStackIn)

NodeDef(name="tech-stack", agent="tech-stack-analyst",
        input_schema="TechStackIn",        # typed IN
        result_schema="TechStackResult",   # typed OUT
        inputs={"product_key": "{product_key}", "report": "{run_dir}/tech-stack.md"})
```

`input_schema` is the mirror of `result_schema`: same accepted forms, validated
against the resolved work order before the agent runs, and surfaced to an
in-process impl as `inv.input_obj`. See
[Type a node's inputs](recipes.md#type-a-nodes-inputs-input_schema) for the
failure semantics.

Working imperatively you can pass the class or dict directly —
`agent_node(..., result_schema=PydanticSchema(TechStackResult))` — the registry
exists so the declarative surface stays serializable.

The schema is injected into the agent's prompt and the result is validated
(never fails the run — check `result["_result_valid"]` in a gate if you need to
act on it). See [result-schema.md](../design/result-schema.md).

## Publish a value to downstream nodes (`exports`) {#exports}

A node can discover a value and hand it to the nodes that follow. `exports`
merges values from the node's `result` into the run-scoped param store, so
downstream nodes template `{name}` against them.

```python
# copy result fields into params (the key renames)
NodeDef(name="readiness", agent="readiness-check", result_schema="ReadinessResult",
        exports={"analysis_timestamp": "analysis_timestamp",
                 "pipeline_commit": "pipeline_commit"})

# or register a function for full control; with a result_schema set it receives
# the VALIDATED typed object, so there is no key digging
@registry.export("publish_mode")
def publish_mode(result):
    return {"mode": result.suggested_mode}

NodeDef(name="readiness", agent="readiness-check", result_schema="ReadinessResult",
        export_ref="publish_mode")

# a later node templates the exported value like any other param
NodeDef(name="analyst", agent="analyst", depends_on=["readiness"],
        inputs={"ANALYSIS_TIMESTAMP": "{analysis_timestamp}", "MODE": "{mode}"})
```

Exports are applied after the node settles, so later nodes pick them up
automatically. Pair with a runtime-populated params field (see
[typed params](#typed-required-domain-params)) to give the placeholder a default
and keep it out of the resolved-params summary.

Exports reach nodes that run *after* the publisher, never parallel-group
siblings.

### What a gate sees

Prefer the typed object; fall back to the dict:

- `ctx.obj` — the validated pydantic instance when the node attached a
  `PydanticSchema`, else `None`. Read fields directly: `ctx.obj.ready`.
- `ctx.result` — the raw envelope. The agent's data is at `ctx.result["result"]`,
  schema flags at `["_result_valid"]` / `["_result_errors"]`.

```python
def gate(ctx):
    if ctx.obj is not None:                              # Pydantic: typed fields
        return Restart() if len(ctx.obj.languages) < 2 else Continue()

    if not ctx.result["_result_valid"]:                 # schema check failed
        return Restart(instruction=f"invalid result: {ctx.result['_result_errors']}")

    # the dict / no-schema case
    data = ctx.result.get("result", {})
    return Restart() if not data.get("summary") else Continue()
```

Read `ctx.obj` when you set a schema, `ctx.result` otherwise.

## Choose the execution backend (`--backend`)

The graph runs on a swappable backend. The default is in-process and
Prefect-free: no temporary server, fast startup, one fewer heavy dependency.
Right for everyday single runs.

```bash
# default: in-process backend (nothing to pass)
python my_flow.py run -p product_key=acme

# opt into Prefect for the run UI / history / scheduling / scale
python my_flow.py run -p product_key=acme --backend prefect
# or persist the choice for a session:
export AGENT_FLOW_BACKEND=prefect
```

```python
# the default
run_flow(flow, run_config={"backend": "inprocess"})

# opt-in
run_flow(flow, run_config={"backend": "prefect"})
```

The engine owns the flow logic; the backend only executes. Switching backends
changes how nodes run, never what runs or in what order. Prefect is imported only
when you select it. To add another backend, subclass `FlowBackend` and register
it — nothing in your pipeline changes.

## Watch progress live

The default `run_cli` view prints one line per node transition, then a table:

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

The Agent column is qualified by how the node actually ran: `opencode:analyst`
for a subprocess, `inproc:` for an in-process impl, `mock:` for a substitution —
so under partial mocking you can tell them apart at a glance.

`--show-events/-v` switches to the per-event firehose (`tool edit … (+12/-3)`,
text, `step done (N tokens)`), useful for debugging. Ctrl-C stops cleanly: the
agent's process group is killed and the CLI exits 130.

`--show-diffs` renders each edit/write as a diff block and composes with the
other views:

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
[cli-events.md](../design/cli-events.md).

## Drop to a lower tier for full control

`agent_node` covers the common "one agent, KEY: value inputs" case. When a node
needs something else — two agents in sequence, or a prompt `inputs`/`instructions`
cannot express — write its `run` yourself. Prefer `async def` with `arun_agent`
so supervision runs on the engine's loop; a sync `def` calling `run_agent` works
too, offloaded to a worker thread:

```python
from agent_flow import Node, arun_agent, get_runner

async def run(ctx):
    r = await arun_agent(agent="my-agent", prompt="...", run_dir=ctx.run_dir,
                          runner=get_runner(ctx.params.get("runtime", "opencode")),
                          control_file=ctx.run_dir / "my-agent.control.json")
    return r.control

Node("custom", run=run, depends_on=("hello",))
```

Hand-written `Node`s and `agent_node`s mix freely in one graph.

### One agent, no engine

The lowest level is a single supervised agent: no graph, no backend, no gates.
`run_agent` spawns it, supervises liveness, kills it if it goes silent, and
returns the verdict it wrote to its control file.

```python
from pathlib import Path

from agent_flow import get_runner, run_agent

run_dir = Path("/tmp/hello-run")
run_dir.mkdir(parents=True, exist_ok=True)

result = run_agent(
    agent="hello-analyst",
    prompt="Write a one-line markdown report to /tmp/hello-run/hello.md",
    run_dir=run_dir,
    runner=get_runner("opencode"),
    # where the agent .md files live (opencode --dir)
    agent_dir=Path("."),
)

# {'status': 'ok', 'agent': 'hello-analyst', ...}
print(result.control)
print(result.control["status"], result.duration_s, result.tokens)
```

The completion protocol is injected for you, so the agent knows where to write
its verdict — the same contract the engine relies on. `arun_agent` is the async
twin. Use this to script a single agent, or as the leaf of a flow you write
yourself; see `examples/custom_flow.py`.
