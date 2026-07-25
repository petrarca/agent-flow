---
type: Guide
title: Getting started with agent-flow
description: Install agent-flow and write your first pipeline, from a trivial mock run to real opencode agents.
tags: [agent-flow, getting-started, tutorial, agent_node, build_flow]
timestamp: 2026-07-23T08:54:40Z
---

# Getting started

This walks through writing your first pipeline: one agent, then two, first with
the token-free **mock** runtime, then with real **opencode**. Every snippet here
has been run as written.

## 1. Install

Requires Python 3.14+ and [`uv`](https://docs.astral.sh/uv/). For real (not
mock) runs, `opencode` must be on `PATH` and configured with model access.

```bash
# core only (programmatic build_flow on the in-process backend):
uv add "agent-flow @ git+https://github.com/petrarca/agent-flow"
# with the CLI (run_cli + live display) — the typical interactive install:
uv add "agent-flow[cli] @ git+https://github.com/petrarca/agent-flow"
# add the opt-in Prefect backend too:
uv add "agent-flow[cli,prefect] @ git+https://github.com/petrarca/agent-flow"
```

**Lean core, optional extras.** The default install is small: pydantic,
pydantic-settings, pyyaml, jsonschema, python-dotenv — enough to declare a
pipeline and run it in-process on the default **in-process backend**, with typed
result output, typed run-parameter models, and YAML/`--config` support. The
heavy pieces are opt-in extras that mirror the runtime seams:

- `agent-flow[cli]` — typer + rich, for the reusable `run_cli` command and the
  live event / status-table display.
- `agent-flow[prefect]` — Prefect, for the opt-in `--backend prefect` (run UI,
  scheduling, scale). The default in-process backend needs none of it.
- `agent-flow[all]` — both. `agent-flow[dev]` implies `[all]` plus the toolchain.

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

```python
# flow.py
from agent_flow import load_env
load_env()

from agent_flow import agent_node, build_flow
from agent_flow.gates import require_file

nodes = [
    agent_node(
        "hello",
        "hello-analyst",
        inputs={"REPORT": "{run_dir}/hello.md"},
        gate=require_file("hello.md"),  # re-try if the report didn't land
    ),
]

pipeline = build_flow(nodes, name="hello")
result = pipeline(runtime="mock")   # no run_dir -> a temp dir under <temp>/agent-flow/ (logged)
print(result)  # {'hello': 'ok'}
```

`load_env()` loads a `.env` file into the process environment so every agent
subprocess inherits it. Call it first, before building the flow, exactly as
shown. The default **in-process backend** needs no further setup.

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
- The **gate** `require_file("hello.md")` looks under the same `run_dir`
  automatically: a bare relative path is joined onto `run_dir`. You can also
  write `require_file("{run_dir}/hello.md")` — both forms resolve to the same
  file, and both may use run params (`require_file("{product_key}.md")`). Use
  whichever reads best; they are equivalent.

### Two directories: `run_dir` vs `agent_dir`

These are independent, and it matters once your agents live somewhere other than
where outputs go:

- **`run_dir`** — where THIS RUN reads/writes: control sidecars, and the base
  for relative artifact paths. It is not a cwd and not where agents are defined.
- **`agent_dir`** — where the runtime finds AGENT DEFINITIONS (opencode's
  `.opencode/agent/*.md`). Passed to opencode as `--dir`. Set a default with
  `build_flow(agent_dir="…")` and override per node with
  `agent_node(..., agent_dir="…")`. Both are templated (`{repo}/…`).

In the toy example these happen to be the same tree; in a real pipeline they
usually differ — e.g. agents in your pipeline repo, outputs in a product folder:

```python
build_flow(nodes, agent_dir="{repo}/pipelines/tech-assessment")   # agents here
pipeline(run_dir="{repos_root}/{product_key}/output",             # outputs here
         repos_root="/data/products", product_key="acme", repo="/work/pipeline", runtime="opencode")
```

Run it: `python flow.py`. You should see `{'hello': 'ok'}` and a `hello.md`
file under the temp `run_dir` logged at flow start (pass an explicit `run_dir`
to keep it). This ran the packaged **mock agent** — no opencode process, no
tokens — so you can develop your graph shape before spending anything on real
agents.

## 4. Switch to a real agent

Change one argument:

```python
result = pipeline(runtime="opencode")   # or pipeline(run_dir="out", ...)
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
nodes = [
    agent_node("hello", "hello-analyst", inputs={"REPORT": "{run_dir}/hello.md"}, gate=require_file("hello.md")),
    agent_node(
        "hello-verify", "hello-verifier",
        depends_on=("hello",),
        inputs={"REPORT": "{run_dir}/hello.md"},
        criticality="degrade",  # a failed check shouldn't stop the whole run
    ),
]
```

`hello-verifier` reads `hello.md` (via its own `.md` instructions) and reports
its own status. If it should be able to ask for `hello` to be redone, see
[recipes.md — "a verifier that can trigger a re-run"](recipes.md#a-verifier-that-can-trigger-a-re-run).

## Where to go next

- [writing-agents.md](writing-agents.md) — what your agent `.md` needs to do
  (the control-file contract, from an agent-author's point of view).
- [recipes.md](recipes.md) — gates, re-run loops, parallel steps, typed output,
  live events, passing a run-wide brief, and dropping to a lower tier for full
  control.
- [`docs/design/orchestrator/index.md`](../design/orchestrator/index.md) — the
  architecture and why it's shaped this way, if you want the full picture.
