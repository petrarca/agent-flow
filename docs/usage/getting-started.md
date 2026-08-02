---
type: Guide
title: Getting started with agent-flow
description: Install agent-flow and write your first pipeline, from a token-free mocked run to real opencode agents.
tags: [agent-flow, getting-started, tutorial, flowdef, run_flow]
timestamp: 2026-07-26T00:00:00Z
---

# Getting started

Write a pipeline of one agent, then two — first token-free, then with real
opencode.

## Install

Requires Python 3.14+ and [`uv`](https://docs.astral.sh/uv/). Real runs also need
`opencode` on `PATH`, configured with model access.

```bash
# typical: CLI + live display
uv add "petrarca-agent-flow[cli]"

# core only, programmatic use
uv add "petrarca-agent-flow"

# + the opt-in Prefect backend
uv add "petrarca-agent-flow[cli,prefect]"
```

The core install is small (anyio, loguru, pydantic, pydantic-settings, pyyaml,
jsonschema, python-dotenv, universal-pathlib). `[cli]` adds typer + rich,
`[prefect]` adds the opt-in backend, `[all]` adds both. Using a feature without
its extra raises a message naming the extra to install.

## 1. Write an agent

agent-flow runs agents; it does not write them. Create
`.opencode/agent/hello-analyst.md`:

```markdown
---
description: Says hello and writes a one-line report.
mode: primary
permission: { edit: allow, bash: deny, webfetch: deny }
---

# Hello analyst

Write a one-line markdown report to the `REPORT` path (Write tool):
`# Hello from agent-flow`
```

You never write the "how to signal completion" part — the library injects it.
See [writing-agents.md](writing-agents.md) for the contract.

## 2. Declare the pipeline

A pipeline is data: a `FlowDef` of `NodeDef`s, validated before it runs.

```python
# flow.py
from agent_flow import load_env
load_env()

from agent_flow import FlowDef, NodeDef, FlowRegistry, run_flow

flow = FlowDef(
    name="hello",
    nodes=[
        NodeDef(
            name="hello",
            agent="hello-analyst",
            inputs={"REPORT": "{run_dir}/hello.md"},
            gate="require_file",
            gate_args={"path": "{run_dir}/hello.md"},
        ),
    ],
)

registry = FlowRegistry()

@registry.mock_agent("hello-analyst")
def hello_mock(inv, ctx):
    ctx.write_file("{run_dir}/hello.md", "# Hello from agent-flow")
    return {"status": "ok"}

result = run_flow(flow, registry=registry, mock_agents=True)
# {'hello': NodeOutcome(status='ok', ...)}
print(result)
```

Run `python flow.py`. The `mock_agent` stands in for the real agent, so this
costs no tokens — develop your graph shape before spending anything.

`load_env()` loads `.env` into the environment every agent subprocess inherits;
call it first. Without a `run_dir` the run uses a temp directory, logged at
start — pass one explicitly for output you want to keep.

Already on an event loop (FastAPI, a notebook)? Use `await arun_flow(flow, ...)`
— same arguments, same result.

### Run it from a CLI instead

Hand the same flow to `run_cli` and you get `run`, `flow nodes` and `version`
commands, with flags for params, mocking, run dir and partial runs:

```python
from agent_flow.cli import run_cli
# then: python flow.py run --mock-agents
run_cli(flow, registry=registry)
```

## 3. Switch to the real agent

Drop the mock:

```python
result = run_flow(flow, runtime="opencode")
```

`hello-analyst` now runs as a supervised opencode subprocess. The gate, the
graph and the completion contract are unchanged.

> Start real opencode runs from a normal shell, **outside** an opencode session —
> a nested session raises `UnknownError`.

## 4. Add a second node

A verifier is not a special concept — it is a node that depends on the first:

```python
flow = FlowDef(name="hello", nodes=[
    NodeDef(name="hello", agent="hello-analyst",
            inputs={"REPORT": "{run_dir}/hello.md"},
            gate="require_file", gate_args={"path": "{run_dir}/hello.md"}),
    NodeDef(name="hello-verify", agent="hello-verifier",
            depends_on=["hello"],
            inputs={"REPORT": "{run_dir}/hello.md"},
            criticality="degrade"),
])
```

`criticality="degrade"` keeps a failed check from stopping the run. To let the
verifier send the flow back to `hello`, see
[a verifier that can trigger a re-run](advanced-recipes.md#a-verifier-that-can-trigger-a-re-run).

## run_dir and agent_dir

Two directories, easy to confuse:

- **`run_dir`** — where this run writes: control sidecars and the base for
  relative artifact paths. One per run.
- **`agent_dir`** — where the runtime finds agent definitions
  (`.opencode/agent/*.md`), passed to opencode as `--dir`.

`{run_dir}` and any run param are substituted in `inputs` before the agent sees
them, so the agent receives absolute paths. Use the same `{run_dir}/...` value in
a gate's `path` as in the node's `inputs` so both point at one file.

`agent_dir` is run configuration, not pipeline data — supply it via
`run_config=`, `--agent-dir` or the environment. The opencode runner also probes
for `.opencode/` in the current directory and its parents, so running from your
project usually needs nothing:

```python
run_flow(flow,
         run_dir="{repos_root}/{product_key}/output",
         run_config={"agent_dir": "{repo}/pipelines/tech-assessment"},
         repos_root="/data/products", product_key="acme", repo="/work/pipeline",
         runtime="opencode")
```

## Next

- [writing-agents.md](writing-agents.md) — the contract your agent `.md` must follow.
- [recipes.md](recipes.md) — the CLI, gates, re-run loops, parallel steps, typed output.
- [advanced-recipes.md](advanced-recipes.md) — parallel steps, exports, partial runs, backends.
