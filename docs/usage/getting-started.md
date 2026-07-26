---
type: Guide
title: Getting started with agent-flow
description: Install agent-flow and write your first pipeline, from a token-free mocked run to real opencode agents.
tags: [agent-flow, getting-started, tutorial, flowdef, run_flow]
timestamp: 2026-07-26T00:00:00Z
---

# Getting started

This walks through writing your first pipeline: one agent, then two, first
token-free with the `--mock-agents` mode, then with real **opencode**. Every
snippet here has been run as written.

## 1. Install

Requires Python 3.14+ and [`uv`](https://docs.astral.sh/uv/). For real runs
(without `--mock-agents`), `opencode` must be on `PATH` and configured with
model access.

The PyPI distribution is `petrarca-agent-flow` (the import name is `agent_flow`).

```bash
# core only (programmatic build_flow on the in-process backend):
uv add "petrarca-agent-flow"
# with the CLI (run_cli + live display) — the typical interactive install:
uv add "petrarca-agent-flow[cli]"
# add the opt-in Prefect backend too:
uv add "petrarca-agent-flow[cli,prefect]"
# or pin to the git repo instead of PyPI:
uv add "petrarca-agent-flow[cli] @ git+https://github.com/petrarca/agent-flow"
```

**Lean core, optional extras.** The default install is small: pydantic,
pydantic-settings, pyyaml, jsonschema, python-dotenv — enough to declare a
pipeline and run it in-process on the default **in-process backend**, with typed
result output, typed run-parameter models, and YAML/`--config` support. The
heavy pieces are opt-in extras that mirror the runtime seams:

- `petrarca-agent-flow[cli]` — typer + rich, for the reusable `run_cli` command
  and the live event / status-table display.
- `petrarca-agent-flow[prefect]` — Prefect, for the opt-in `--backend prefect`
  (run UI, scheduling, scale). The default in-process backend needs none of it.
- `petrarca-agent-flow[all]` — both. `petrarca-agent-flow[dev]` implies `[all]`
  plus the toolchain.

Using a feature without its extra raises a clear message naming the extra to
install.

(If you're working inside this repo instead of consuming it as a dependency,
see the root [`README.md`](../../README.md) for `task install`.)

## 2. Write one agent

`agent-flow` supervises agent **subprocesses** — it does not write agent
instructions for you. Create `.opencode/agent/hello-analyst.md`:

```markdown
---
description: Says hello and writes a one-line report.
mode: primary
permission: { edit: allow, bash: deny, webfetch: deny }
---

# Hello analyst

Write a one-line markdown report to the `REPORT` path (Write tool):
`# Hello from agent-flow`

(Your prompt also includes a completion-protocol block telling you how to
signal you're done — follow it after the task above.)
```

That's it — you never write the "how to signal completion" part; the library
injects it. See [writing-agents.md](writing-agents.md) for the full contract.

## 3. Declare and run the pipeline

You declare the pipeline as **data** — a `FlowDef` of `NodeDef`s — and run it.
This is the recommended surface: no callables, serializable, validated before it
runs. (There's also a lower-level imperative form, `agent_node`; see the note at
the end.)

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
            gate="require_file",              # a built-in gate, by name
            gate_args={"path": "{run_dir}/hello.md"},  # re-try if the report didn't land
        ),
    ],
)

# A mock_agent: a deterministic, no-token stand-in for the "hello-analyst" agent.
# Registered by AGENT NAME on a FlowRegistry; used only under --mock-agents.
registry = FlowRegistry()

@registry.mock_agent("hello-analyst")
def hello_mock(inv, ctx):
    ctx.write_file("{run_dir}/hello.md", "# Hello from agent-flow")
    return {"status": "ok"}

result = run_flow(flow, registry=registry, mock_agents=True)  # no run_dir -> a temp dir under <temp>/agent-flow/ (logged)
print(result)  # {'hello': NodeOutcome(status='ok', ...)}
```

`load_env()` loads a `.env` file into the process environment so every agent
subprocess inherits it. Call it first. `run_flow` compiles the FlowDef and runs
it in one call; the default **in-process backend** needs no further setup.

`--mock-agents` (the `mock_agents=True` run param) is a **substitution mode**,
not a runtime: for any node whose agent has a registered `mock_agent`, that
deterministic behaviour runs in-process — no subprocess, no tokens. Nodes without
one still run for real (partial mocking). The behaviour's `ctx` is a small
toolset: `ctx.write_file(path, content)` / `ctx.read_file(path)` (with the same
`{run_dir}`/`{param}` templating as `inputs`) and `ctx.input(key)` to read a
structured work-order value.

Run it: `python flow.py`. You should see `{'hello': ...ok...}` and a `hello.md`
file under the temp `run_dir` logged at flow start (pass an explicit `run_dir`
to keep it). This ran your registered **mock_agent** — no opencode process, no
tokens — so you can develop your graph shape before spending anything on real
agents.

**Prefer a CLI?** Hand the same `flow` (and `registry`) to `run_cli` instead of
calling `run_flow` — you get a `run` command with flags (`-p KEY=VALUE`,
`--mock-agents`, `--run-dir`, `--start-from`, `--only`, live display), a
`flow nodes` command that prints the graph, and a `version` command. It needs the
`[cli]` extra:

```python
# flow.py
from agent_flow.cli import run_cli
run_cli(flow, registry=registry)   # then: python flow.py run --mock-agents
```

A gate is referenced **by name** (`gate="require_file"`) with its config as data
(`gate_args`). `require_file` / `rerun_on_signal` / `rerun_on_named` are built in;
to plug in your own logic you register a function on a `FlowRegistry` and
reference it by name — see [recipes.md](recipes.md) and
[the FlowDef design doc](../design/orchestrator/flowdef.md).

### Where does `run_dir` come from, and how does the gate find the file?

- **`run_dir`** is the argument you pass when you *run* the pipeline
  (`pipeline(run_dir="out", ...)`). The library resolves it to an absolute
  path, creates it, and threads it to every node. There's one `run_dir` per run.
  **If you pass nothing** (or `""`), a fresh temp dir is created under
  `<system temp>/agent-flow/<name>-<timestamp>-<rand>` and logged at flow start
  — convenient for demos, but **ephemeral** (the OS may purge it), so pass an
  explicit `run_dir` for output you need to keep.
- In **`inputs`**, `{run_dir}` (and any run param like `{product_key}`) is
  substituted before the value is handed to the agent — so the agent receives an
  absolute `REPORT` path and writes exactly there. (Absolute matters — see the
  gotcha in [writing-agents.md](writing-agents.md).)
- The **gate** `require_file` (via `gate_args={"path": "{run_dir}/hello.md"}`) checks
  the same file the agent was told to write. Use the explicit `{run_dir}/...` form
  so `path=` matches the node's `inputs=` value exactly — both resolve to the same
  file, and both may use run params (`"{product_key}.md"`). Equivalent; use
  whichever reads best.

### Two directories: `run_dir` vs `agent_dir`

These are independent, and it matters once your agents live somewhere other than
where outputs go:

- **`run_dir`** — where THIS RUN reads/writes: control sidecars, and the base
  for relative artifact paths. It is not a cwd and not where agents are defined.
- **`agent_dir`** — where the runtime finds AGENT DEFINITIONS (opencode's
  `.opencode/agent/*.md`). Passed to opencode as `--dir`. Set it on the FlowDef
  (`FlowDef(agent_dir="…")`) and override per node with `NodeDef(agent_dir="…")`.
  Both are templated (`{repo}/…`).

In the toy example these happen to be the same tree; in a real pipeline they
usually differ — e.g. agents in your pipeline repo, outputs in a product folder:

```python
flow = FlowDef(name="hello", agent_dir="{repo}/pipelines/tech-assessment", nodes=[...])  # agents here
run_flow(flow, run_dir="{repos_root}/{product_key}/output",                              # outputs here
         repos_root="/data/products", product_key="acme", repo="/work/pipeline", runtime="opencode")
```

## 4. Switch to a real agent

Drop `--mock-agents` (and its registry) so the node runs the real `hello-analyst`
opencode agent instead of the mock:

```python
result = run_flow(flow, runtime="opencode")   # or run_flow(flow, run_dir="out", ...)
```

Now `hello-analyst.md` (the real one you wrote in step 2) runs as a supervised
`opencode` subprocess. Everything else — the gate, the graph, the control-file
protocol — is unchanged.

> Run real-opencode pipelines from a normal shell **outside** an opencode
> session (a nested opencode session raises `UnknownError`).

## 5. Add a second node

A "verifier" is not a special concept — it's just another node that depends on
the first:

```python
flow = FlowDef(name="hello", nodes=[
    NodeDef(name="hello", agent="hello-analyst", inputs={"REPORT": "{run_dir}/hello.md"},
            gate="require_file", gate_args={"path": "{run_dir}/hello.md"}),
    NodeDef(
        name="hello-verify", agent="hello-verifier",
        depends_on=["hello"],
        inputs={"REPORT": "{run_dir}/hello.md"},
        criticality="degrade",  # a failed check shouldn't stop the whole run
    ),
])
```

(The same pipeline can be written imperatively with `agent_node(...)` if you
prefer building runtime nodes directly — see `examples/imperative.py`. FlowDef is
the recommended surface.)

`hello-verifier` reads `hello.md` (via its own `.md` instructions) and reports
its own status. If it should be able to ask for `hello` to be redone, see
[recipes.md — "a verifier that can trigger a re-run"](recipes.md#a-verifier-that-can-trigger-a-re-run).

## Where to go next

- [writing-agents.md](writing-agents.md) — what your agent `.md` needs to do
  (the control-file contract, from an agent-author's point of view).
- [recipes.md](recipes.md) — the `run_cli` CLI (`run` / `flow nodes` / `version`, flags,
  `--start-from`/`--only`), gates, re-run loops, parallel steps, typed output,
  live events, passing a run-wide brief, and dropping to a lower tier for full
  control.
- [`docs/design/orchestrator/index.md`](../design/orchestrator/index.md) — the
  architecture and why it's shaped this way, if you want the full picture.
