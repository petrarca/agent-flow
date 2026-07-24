---
type: Guide
title: Recipes
description: Task-oriented how-tos for agent-flow — gates, re-run loops, parallel steps, typed output, live events, and dropping to a lower tier.
tags: [agent-flow, recipes, how-to, gates, parallel, typed-output, cli]
timestamp: 2026-07-23T08:54:40Z
---

# Recipes

Short, task-oriented how-tos. Each assumes you've read
[getting-started.md](getting-started.md). All snippets have been run as shown.

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
# generic settings as flags + arbitrary DOMAIN params via -p/--param KEY=VALUE:
python -m my_pkg.flow -p product_key=my-product -p repos_root=/tmp/repos \
    --runtime opencode --run-dir "{repos_root}/{product_key}/output" -i "use code-graph"

# or put it all in a YAML config file:
python -m my_pkg.flow --config run.yml
```

```yaml
# run.yml — generic settings at top level, a params: section for domain values
runtime: opencode
run_dir: "{repos_root}/{product_key}/output"
agent_dir: /work/pipelines/tech-assessment
llm_concurrency: 2
instructions: |
  Experimental code-graph support is available; use it alongside RAG.
params:              # arbitrary — the library has no domain concepts
  product_key: my-product
  repos_root: /tmp/repos
```

Precedence: **CLI flag > config file > default**. `params` are passed to
`pipeline(**params)` and are usable as `{name}` templates in `inputs`, `context`,
and paths — so `-p product_key=my-product` makes `{product_key}` resolve
everywhere. There is no `--product` option (or any domain option) built in;
`--param` is the generic protocol for all of them.

Prefer your own CLI? `run_cli` is optional — build any CLI you like and call
`build_flow(...)` / `pipeline(**params)` directly.

## Check that a step actually produced its file

```python
from agent_flow.gates import require_file

agent_node("hello", "hello-analyst", inputs={"REPORT": "{run_dir}/hello.md"},
           gate=require_file("hello.md"))
```

If the control sidecar says `ok` but `hello.md` is missing or empty, the node is
re-run (bounded by `max_cycles`, default 1). See
[gates.md](../design/orchestrator/gates.md).

## A verifier that can trigger a re-run

There's no built-in "verifier" concept — it's a plain node that depends on the
step it checks, with a gate that can send the flow back:

```python
from agent_flow.gates import require_file, rerun_on_signal

nodes = [
    agent_node("hello", "hello-analyst", inputs={"REPORT": "{run_dir}/hello.md"},
               gate=require_file("hello.md")),
    agent_node("hello-verify", "hello-verifier",
               depends_on=("hello",),
               inputs={"REPORT": "{run_dir}/hello.md"},
               criticality="degrade",           # a failed check shouldn't halt the run
               gate=rerun_on_signal(target="hello")),
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
python my_flow.py --instructions "Experimental code-graph support is available; use it alongside RAG where sensible."
```

(Wire `--instructions`/`-i` yourself with Typer/argparse, as the tech-assessment
example does — see `examples/tech_assessment/tech_flow.py`.) It's injected into
**every** agent's prompt, after the control protocol. See
[input-plane.md](../design/orchestrator/input-plane.md).

## Give one node an extra, specific instruction

Additive to the run-wide brief, for one node only:

```python
agent_node("tech-stack", "tech-stack-analyst",
           inputs={"REPORT": "{run_dir}/tech-stack.md"},
           instructions="List concrete versions where known; prefer a compact table.")
```

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

# Or, with the `pydantic` extra:
from pydantic import BaseModel
from agent_flow.schema_pydantic import PydanticSchema

class TechStackResult(BaseModel):
    summary: str
    languages: list[str]

agent_node("tech-stack", "tech-stack-analyst", result_schema=PydanticSchema(TechStackResult))
```

The schema is injected into the agent's prompt and the result is validated
(never fails the run — check `result["_result_valid"]` in a gate if you need to
act on it). See [result-schema.md](../design/orchestrator/result-schema.md).

### What a gate sees

Two simple rules:

- **`ctx.result["result"]`** — the agent's result **dict**, always present. If a
  schema was attached, this data was validated (see `_result_valid` /
  `_result_errors`).
- **`ctx.result["_result_obj"]`** — the **Pydantic model instance**, *only* when
  you attached a `PydanticSchema`; otherwise `None` (a dict schema adds no new
  object — the validated data is the dict already in `result`).

```python
def gate(ctx):
    if not ctx.result["_result_valid"]:                 # schema check failed
        return Restart(note=f"invalid result: {ctx.result['_result_errors']}")

    obj = ctx.result.get("_result_obj")                 # a model, or None
    if obj is not None:                                 # Pydantic: typed fields
        return Restart() if len(obj.languages) < 2 else Continue()

    data = ctx.result.get("result", {})                 # dict/no-schema case
    return Restart() if not data.get("summary") else Continue()
```

In short: **the dict is always in `result`; `_result_obj` is the model if you
gave one, else `None`.**

## See what an agent is doing live

Requires the `cli` extra (`pip install agent-flow[cli]`):

```python
from agent_flow.cli import event_printer, get_console

console = get_console()
pipeline = build_flow(nodes, name="my-pipeline",
                       on_event_factory=lambda agent: event_printer(agent, console=console))
```

Prints one readable line per event (`tool write /work/report.md`, the agent's
text, `step done (N tokens)`) alongside the normal Prefect logs. See
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
`run_agent` inside your own Prefect flow (Tier 2 — see `examples/toy_pipeline`),
or outside Prefect altogether (Tier 1). See
[index.md](../design/orchestrator/index.md) for the three tiers.
