# Agent flow

Deterministic orchestration of **coding agents** — agentic CLI tools like
**OpenCode** (primary target), Claude Code or Codex, which run as their own
process with their own agent loop, tools and model access. Not in-process agent
frameworks like PydanticAI or LangGraph, where you build the agent inside your
own program.

`agent-flow` replaces the fragile "LLM orchestrator agent" pattern — where a
model is asked to sequence the stages and inevitably hangs, loops, or "loses the
thread" — with a **deterministic engine** that runs those agents as a graph and
supervises each one as an external process.

## Why agent-flow

1. **Deterministic orchestration.** The control flow is plain Python the engine
   executes — a dependency graph with parallel fan-out and bounded backward
   jump-backs. No model decides what runs next, so the same inputs run the same
   stages in the same order.

2. **Supervised subprocess agents.** A CLI agent can hang, crash, or misreport.
   agent-flow supervises by **liveness** (killed only when it goes silent, not on
   a wall-clock cap) and takes its verdict from a JSON **control sidecar** the
   agent writes — no prose-parsing, no silently-accepted failure.

3. **A controlled input plane.** Each prompt is composed from ordered channels
   (completion protocol → run context/brief → node context/instructions → work
   order). The engine *injects file content*, so an agent physically has the rules
   instead of being told to go read them.

4. **Runtime- and backend-agnostic.** `AgentRunner` abstracts the agent CLI
   (OpenCode today; Claude Code, Codex next); `FlowBackend` abstracts execution
   (in-process by default, opt-in Prefect). Everything else is written once.

5. **A programming model for *external* agents.** Workflow engines (Prefect,
   Airflow) run graphs but know nothing about agents. Agent frameworks
   (PydanticAI, LangGraph) know agents but run them **in-process**. agent-flow
   gives that same model — typed input/output, context plane, gates, bounded
   re-runs — to agents running as **external processes**. (In-process nodes work
   too, in the same graph.)

## Feature shortlist

- **Declarative FlowDef surface** — author a pipeline as DATA: a `FlowDef` of
  `NodeDef`s (pydantic, serializable to JSON/YAML, validated before it runs).
  Gates/exports/runs/schemas are referenced BY NAME and resolved via a
  `FlowRegistry`; `run_flow(flow, …)` (or `await arun_flow(flow, …)`) runs it,
  `run_cli(flow)` gives a CLI. It compiles to the same runtime nodes as the
  lower-level `agent_node` form.
- **Flow engine** — `depends_on` dependencies, `parallel_group` fan-out, a
  fail-fast plan (cycles/unknown deps caught at build time); with gates it is a
  flow (not a pure DAG — see jump-back below).
- **Gates** — a post-node decision returning `Continue` / `Stop` / `Restart` /
  `GoTo`. A gate is `(ctx, **config) -> Directive`, referenced by name with its
  config as data; built-ins `require_file`, `stop_if`
  are seeded, or register your own on a `FlowRegistry` (plus observing lifecycle
  hooks: `before_node`/`after_node`/`on_error`/`before_group`/`after_group`).
- **Re-runs as jump-back** — a re-run rewinds to the named node and re-flows
  *forward* from there (re-running it + everything downstream), backward-only,
  bounded by `max_cycles`.
- **Start partway** — `--start-from NODE` (or a parallel-group) enters the flow
  at a chosen node, skipping upstream, to iterate on a late stage.
- **Run one node** — `--only NODE` (or a parallel-group) runs exactly that one
  node and stops (skips everything else); the surgical complement to
  `--start-from`. Mutually exclusive with it.
- **Multi-command CLI** — the reusable `run_cli` is a subcommand app: `run`
  executes the pipeline; `flow nodes` prints it in execution order (node → agent,
  deps, parallel group, gate) to discover `--only`/`--start-from` targets;
  `version` prints the pipeline's version (your app version, if you pass one, plus
  the agent-flow version).
- **Liveness supervision** — idle-timeout (not wall-clock) kill, process-group
  termination.
- **Control sidecar** — a per-node JSON envelope the agent writes; the engine
  reads status/telemetry from it. Deliberately no `artifact` field — outputs are
  the files the agent was told to write.
- **Typed agent output** — an optional `result_schema` (pydantic model or JSON
  schema) injected into the prompt and validated on return; a gate can decide on
  typed fields.
- **Run-context service + `exports`** — a run-scoped, thread-safe store of the
  open domain params; a node can `exports` values from its result into it so
  **downstream** nodes template them (e.g. a readiness check publishing captured
  provenance to every later agent).
- **The input plane** — ordered prompt composition with **content injection** of
  context files/globs, `{param}` templating, a per-run brief (`-i` / file), and
  per-node run-time instructions (`--instruct NODE=…` / config, additive last-word).
- **Agent execution seam** — `AgentExecutor` (ABC; `async def run`).
  `SubprocessExecutor`'s per-runtime wire details are an `AgentRunner` strategy
  (OpenCode today, Claude Code stubbed) with per-runner preflight checks and an
  `AgentRunnerInfo` doctor view. An **in-process** agent (e.g. PydanticAI) skips
  the subprocess/sidecar entirely — a direct call returning a typed object into
  the same result contract — attached via `agent_node(impl=…)` or
  `registry.agent_impl(name)` + `NodeDef.impl_ref`. The impl may be `async def`
  (awaited inline on the loop) or plain `def` (a blocking sync impl is offloaded
  to a worker thread so it never stalls the loop).
- **Mock agents for tests & dev (`--mock-agents`)** — a substitution MODE, not a
  runtime: register a deterministic `mock_agent(inv, ctx) -> envelope` by agent
  name (`FlowRegistry.mock_agent`), and any node running that agent executes it
  via `MockExecutor` instead — no tokens, no subprocess. Un-mocked nodes still run
  for real (partial mocking).
- **Runner-agnostic live display** — the runner normalizes each event into neutral
  fields (`kind`/`title`/`detail`/`status`/`diff`); the CLI renders them (status
  colors + rich token highlighting) with zero runtime-specific knowledge.
  Node-labeled progress lines, an end-of-run results table, and optional
  `--show-diffs` edit/write diffs (`--diff-style unified|split`).
- **Settings** — `RunConfig` (pydantic-settings, `AGENT_FLOW_*`) with a strict
  precedence chain (CLI > env > .env > `--config` > `run_config=` > default);
  `--config` takes a file path or inline JSON and is repeatable + deep-merged.
  Domain params are typed by the flow's own `params_schema` (missing required →
  fail fast, exit 2).
- **Async-first, sync-friendly** — the core runs on
  [`anyio`](https://anyio.readthedocs.io/), so you can embed a flow in your own
  event loop (`await arun_flow(...)` in a FastAPI handler) and async agent
  libraries need no bridge. Additive, not a migration: `run_flow` / `run_cli` /
  `run_agent` keep their blocking signatures, and every consumer callable —
  impls, gates, exports, hooks — may be sync **or** async.
- **Pluggable execution backend** — `FlowBackend` (ABC): a Prefect-free
  **InProcessBackend** (default; an `anyio` task group for parallel fan-out +
  `anyio.Semaphore` for the concurrency limit + stdlib logging, no temp server)
  or an opt-in **PrefectBackend** (`--backend prefect` / `build_flow(...,
  backend="prefect")`) for the run UI, scheduling, and scale. The core
  primitives + flow logic stay Prefect-free (import-isolation-guarded).
- **Three levels of abstraction** — from one supervised agent up to a declared
  graph (below).

## Levels of abstraction

Three ways in, each usable on its own — most consumers only need the first.

- **Declare the graph.** A `FlowDef` (data), or `agent_node` + `build_flow`. One
  node per agent; the library builds the prompt, sidecar path and flow.
  `examples/declarative.py`, `examples/imperative.py`.
- **Write your own flow.** Call `run_agent` as the leaf of a hand-written flow.
  `examples/custom_flow.py`.
- **Run one supervised agent.** `run_agent`: spawn, liveness-supervise, kill,
  read the sidecar verdict. Backend-free.

Diagram and details:
[`docs/usage/index.md`](docs/usage/index.md#layering-high-level--low-level).

## Example — a two-node flow

An analyst writes a report; a verifier checks it and can bounce the flow back to
re-run the analyst. The pipeline is pure DATA — no callables, serializable,
validated before it runs — and gates are referenced by name.

```python
from agent_flow import FlowDef, NodeDef, run_flow

flow = FlowDef(
    name="tech",
    nodes=[
        NodeDef(
            name="tech-stack",
            agent="tech-stack-analyst",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
            gate="require_file",
            gate_args={"path": "{run_dir}/tech-stack.md"},
        ),
        NodeDef(
            name="tech-stack-verify",
            agent="tech-stack-verifier",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
            depends_on=["tech-stack"],
            criticality="degrade",
            rerun_targets=["tech-stack"],   # the agent may ask to re-run tech-stack
        ),
    ],
)

run_flow(flow, product_key="acme", runtime="opencode")
# …or, on an event loop:  await arun_flow(flow, product_key="acme", runtime="opencode")
```

Hand the same `flow` to `run_cli(flow)` for a `run` / `flow nodes` / `version`
CLI. Add `params_schema=` to declare and validate the params it needs; put
non-portable settings (`agent_dir`, backend, timeouts) in `run_config=` /
`--config`, never on the `FlowDef`. Walk through it properly in
[getting started](docs/usage/getting-started.md).

### Hooking your own logic

Write a function, register it on a `FlowRegistry`, reference it from a node BY
NAME — the node stays pure data, your code lives in the registry:

```python
from agent_flow import FlowDef, NodeDef, FlowRegistry, run_flow
from agent_flow.gates import Continue, Stop

registry = FlowRegistry()

@registry.gate("stack_usable")
def stack_usable(ctx):
    if (ctx.result or {}).get("status") == "error":
        return Stop(reason="tech-stack could not be determined")
    return Continue()

@registry.on("after_node")
def log_outcome(node, outcome):
    print(f"{node.name}: {outcome.status} ({outcome.duration_s:.1f}s)")

flow = FlowDef(name="tech", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst", gate="stack_usable"),
])

run_flow(flow, registry=registry, product_key="acme", runtime="opencode")
```

Same pattern for a result→params export (`@registry.export`) and a custom run
(`@registry.run`) for a node that runs your own code instead of an agent. See
[gates](docs/design/gates.md) and
[recipes](docs/usage/recipes.md).

### Mocking agents for tests & dev

Register a deterministic, no-token stand-in by agent name and run with
`--mock-agents` — any node whose agent has one is routed through it instead of a
real runtime. It is a MODE, not a runtime: a node without a mock still runs for
real (partial mocking). Every example ships a mock mode, so you can run the whole
pipeline without tokens. See
[`docs/design/mock-agent.md`](docs/design/mock-agent.md).

### Where it runs

`build_flow` dispatches execution to a swappable backend: the default
`InProcessBackend` (no Prefect) or the opt-in `PrefectBackend`
(`--backend prefect`) for a run UI and scale. The engine owns all flow logic and
stays backend-free, so the backend can change without touching your pipeline. See
[`docs/design/backend.md`](docs/design/backend.md).

## Install & run

Requires Python 3.14+, [`uv`](https://docs.astral.sh/uv/), and
[`task`](https://taskfile.dev/). For real runs, `opencode` must be on `PATH` and
configured with model access.

**Lean core, optional extras.** The default install is small — enough to declare
a pipeline and run it on the default in-process backend, with typed
params/results and config (anyio, loguru, pydantic, pydantic-settings, pyyaml,
jsonschema, python-dotenv, universal-pathlib). The heavy pieces are opt-in extras
that match the runtime seams.

Installed from PyPI as `petrarca-agent-flow` (the import name is `agent_flow`):

| Install | Adds | Use when |
|---|---|---|
| `petrarca-agent-flow` | core only | programmatic `build_flow` on the in-process backend |
| `petrarca-agent-flow[cli]` | typer, rich | the `run_cli` command + live display |
| `petrarca-agent-flow[prefect]` | prefect | `--backend prefect` (run UI / scale) |
| `petrarca-agent-flow[all]` | cli + prefect | a full interactive install |
| `petrarca-agent-flow[dev]` | all + toolchain | development (implies `[all]`) |

```bash
# typical interactive use
pip install "petrarca-agent-flow[cli]"

# + the Prefect backend
pip install "petrarca-agent-flow[cli,prefect]"

# editable dev install (implies [all])
task install
```

Using a feature without its extra raises a clear message telling you which
extra to install (e.g. `run_cli` without `[cli]`, or `--backend prefect`
without `[prefect]`).

Then walk through your first pipeline, the runnable examples (`declarative.py` /
`imperative.py`, `custom_flow.py` — each with a token-free
`--mock-agents` mode), the `run_cli` flags/params, and writing agents that
cooperate with agent-flow: **[`docs/usage/index.md`](docs/usage/index.md)**.

> Run real OpenCode runs from a normal shell **outside** an OpenCode session (a
> nested OpenCode raises `UnknownError`).

## Develop

`task fct` is the local loop (format + lint + unit tests). The full task list
(`verify`, `test:all`, `test:opencode`, `build`, git hooks) and the coding
standards live in **[`CONTRIBUTING.md`](CONTRIBUTING.md)**.

## Layout

```
src/agent_flow/     the library
  core/             backend-free primitives (run_agent, control protocol,
                    result-schema, context ingestion, env)
  runners/          the agent-execution seam (AgentExecutor: Subprocess + InProcess + Mock)
                    and the subprocess wire adapters (AgentRunner) — OpenCode, …
  backends/         the graph-execution seam (FlowBackend) — inprocess (default), prefect (opt-in)
  cli/              run_cli + neutral event rendering + tables (the [cli] extra)
  engine, gates, node_builder, run_config, run_context, preflight, utils
                    the flow engine, flow-control gates, the one-call node, and
                    the run-time plumbing that ties the seams together
  flowdef/          the declarative FlowDef/NodeDef surface + compile_flow
examples/           declarative.py, imperative.py, custom_flow.py, inprocess.py
docs/design/   the design (start at index.md)
```

Layer order: `utils < runners < core < engine/gates/node_builder < backends < cli`.

## Documentation

- **Using the library** (task-oriented) — install, write your first pipeline,
  write agents that work with agent-flow, and recipes for common tasks:
  [`docs/usage/index.md`](docs/usage/index.md).
- **Design** (the architecture and why) — problem, principles, the layering,
  and one focused document per concept (supervision, control-file, engine,
  gates, node_builder, input-plane, result-schema, backend, cli-events):
  [`docs/design/index.md`](docs/design/index.md).

## Contributing & License

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
workflow and conventions (and [`AGENTS.md`](AGENTS.md) if you use an AI coding
assistant). Licensed under the Apache License 2.0 — see [`LICENSE`](LICENSE).
