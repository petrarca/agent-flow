# Agent flow

Deterministic orchestration of coding-agent pipelines. `agent-flow` replaces the
fragile "LLM orchestrator agent" pattern — where a model is asked to sequence the
stages and inevitably hangs, loops, or "loses the thread" — with a **deterministic
engine** that runs your agents as a graph and supervises each one as an external
process.

## Why agent-flow

Turning a set of coding agents (OpenCode, Claude Code, …) into a dependable
pipeline comes down to five capabilities. agent-flow delivers each directly:

1. **Deterministic orchestration.** The control flow is **plain Python the engine
   executes** — a directed graph with dependencies and parallel fan-out, plus
   bounded backward jump-backs (so a flow/state machine, not a pure DAG): a gate
   can rewind the flow to an earlier node, re-running it and everything
   downstream, bounded by `max_cycles`. No model decides what runs next, so the
   orchestrator cannot hang or improvise the sequence. Given the same inputs, the same stages
   run in the same order.

2. **Reliable execution of a subprocess agent.** A CLI agent (OpenCode, Claude
   Code, …) can hang, crash, or misreport, and nothing durable normally tells you
   which. agent-flow supervises it by **liveness** (not a fixed wall-clock cap) —
   killed only when it goes *silent*, with clean process-group termination — and
   reads its real outcome from a small JSON **control sidecar** the agent writes
   (no prose-parsing). A crashed, stalled, or invalid-output agent is detected and
   handled, not silently accepted; this supervision is the executor's private
   mechanism, never baked into the engine.

3. **Controlled ingestion of context and runtime parameters.** A defined **input
   plane** composes each agent's prompt from ordered channels (completion
   protocol, run-wide context/brief, per-node context/instructions, templated work
   order) — the engine *injects file content*, so an agent physically has the
   rules rather than being told to go read them. Runtime parameters (model,
   liveness timeout, domain params) resolve through one precedence chain
   (CLI > env > .env > YAML > default) and flow through a **run-context service** —
   values can even be published by one node for downstream nodes (`exports`).

4. **Runtime- and backend-agnostic.** The subprocess executor's per-runtime wire
   details are a further **`AgentRunner`** strategy — OpenCode today, Claude Code
   and Codex next — where only "build the command" and "parse the event stream"
   differ. Separately, the **`FlowBackend`** decides *how the graph runs*: a
   Prefect-free **InProcessBackend** (default) or an opt-in **PrefectBackend**
   (`--backend prefect`). The flow, re-runs, gates, input plane, and display layer
   are written once and stay agnostic to both seams.

5. **A unified programming model for *external* agents.** General workflow
   engines (Prefect, Airflow, Dagster, …) can certainly *run* a graph, but they
   offer no programming model for integrating such agentic tools (like OpenCode)
   — no notion of an agent's prompt, injected context, typed result, control
   verdict, or the re-run semantics an agent pipeline needs; you build all of that
   yourself on top of raw tasks. The frameworks that *do* provide that model —
   **PydanticAI (Graph)**, **LangGraph**, and similar — run the agents
   **in-process**: the node *is* an LLM/tool call in
   your Python process. agent-flow gives the same unified model (nodes with typed
   input/output, a controlled context/instruction plane, gates, bounded re-runs)
   but for agents that run as **external processes** — full coding agents like
   OpenCode and Claude Code, supervised as subprocesses. It fills the gap between
   "a workflow engine that runs anything but knows nothing about agents" and "an
   agent framework that knows agents but only in-process". (An in-process executor
   is supported too, so an in-process agent can be a node in the same graph.)

## Feature shortlist

- **Declarative FlowDef surface** — author a pipeline as DATA: a `FlowDef` of
  `NodeDef`s (pydantic, serializable to JSON/YAML, validated before it runs).
  Gates/exports/runs/schemas are referenced BY NAME and resolved via a
  `FlowRegistry`; `run_flow(flow, …)` runs it, `run_cli(flow)` gives a CLI. It
  compiles to the same runtime nodes as the lower-level `agent_node` form.
- **Flow engine** — `depends_on` dependencies, `parallel_group` fan-out, a
  fail-fast plan (cycles/unknown deps caught at build time); with gates it is a
  flow (not a pure DAG — see jump-back below).
- **Gates** — a post-node decision returning `Continue` / `Stop` / `Restart` /
  `GoTo`. A gate is `(ctx, **config) -> Directive`, referenced by name with its
  config as data; built-ins `require_file`, `rerun_on_signal`, `rerun_on_named`
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
- **Agent execution seam** — `AgentExecutor` (ABC). `SubprocessExecutor`'s
  per-runtime wire details are an `AgentRunner` strategy (OpenCode today, Claude
  Code stubbed) with per-runner preflight checks and an `AgentRunnerInfo` doctor
  view. An **in-process** agent (e.g. PydanticAI) skips the subprocess/sidecar
  entirely — a direct call returning a typed object into the same result
  contract — attached via `agent_node(impl=…)` or `registry.agent_impl(name)` +
  `NodeDef.impl_ref`.
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
  precedence chain; domain params typed via a `params_model` (missing required →
  fail fast, exit 2).
- **Pluggable execution backend** — `FlowBackend` (ABC): a Prefect-free
  **InProcessBackend** (default; threadpool + semaphore + stdlib logging, no temp
  server) or an opt-in **PrefectBackend** (`--backend prefect` / `build_flow(...,
  backend="prefect")`) for the run UI, scheduling, and scale. The core
  primitives + flow logic stay Prefect-free (import-isolation-guarded).
- **Three usage tiers** — from one supervised agent up to a declared graph (below).

## Three usage tiers (high level → low level)

Pick the tier that fits; each is usable on its own. Higher tiers are more
declarative; lower tiers give more control.

```
TIER 3  DECLARATIVE      a FlowDef (data) or agent_node() -> build_flow()
  (most declarative)     a runnable flow; one node per agent
        │ composes
TIER 2  PRIMITIVES       call run_agent() as the leaf of YOUR OWN flow
        │ uses
TIER 1  ENGINE CORE      run_agent(): spawn + liveness-supervise + kill + sidecar verdict
  (closest to the metal) runner-agnostic; backend-free
        │ invokes
        AGENT RUNTIME    OpenCode agents (.md) — external, unchanged
```

- **Tier 3 — declare the graph** (a `FlowDef`, or `agent_node` + `build_flow`):
  one node per agent; the library builds the prompt, sidecar path, and flow.
  See `examples/declarative.py` and `examples/imperative.py`.
- **Tier 2 — your own flow**: call `run_agent` as the leaf of a
  hand-written flow. See `examples/custom_flow.py`.
- **Tier 1 — one supervised agent** (`run_agent`): spawn + liveness-supervise +
  kill + read the sidecar verdict. Backend-free.

### Example — a two-node flow (Tier 3, declarative)

A minimal analyst → verifier pipeline: the analyst writes a report; the verifier
checks it and can bounce the flow back to re-run the analyst. This is the
**declarative surface** — a `FlowDef` of `NodeDef`s: pure DATA (no callables),
serializable to JSON/YAML, validated before it runs.

A **node** describes one step: which agent to run, what it depends on, and a gate
(referenced BY NAME — the built-ins `require_file` / `rerun_on_signal`, or your
own registered on a `FlowRegistry`). You describe the graph as data; the engine
executes it — you never write the control flow. Each node's `inputs` (with
`{param}` placeholders resolved from the run params) become the agent's work
order. Nodes can also carry per-node `context=[...]` (file content injected into
the prompt) and `instructions="..."`; run-wide equivalents live on the FlowDef.
See [the input plane](docs/design/orchestrator/input-plane.md).

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
            gate_args={"relpath": "tech-stack.md"},
        ),
        NodeDef(
            name="tech-stack-verify",
            agent="tech-stack-verifier",
            depends_on=["tech-stack"],
            criticality="degrade",
            gate="rerun_on_signal",
            gate_args={"target": "tech-stack"},
        ),
    ],
)

run_flow(flow, product_key="acme", runtime="opencode")
```

Hand `flow` to the reusable CLI instead of calling `run_flow` directly to get
`run_cli(flow)`'s `run` / `flow nodes` / `version` subcommands for free. Pass
`run_cli(flow, version="1.2.0")` to surface your app's version alongside
agent-flow's.

The same pipeline can be written imperatively with `agent_node(...)`, the
lower-level Tier-3 form.

### Hooking your own logic

The built-in gates cover the common cases. To plug in **your own** logic, write a
function, register it on a `FlowRegistry`, and reference it from a node BY NAME —
the node stays pure data, your code lives in the registry:

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

A gate that needs per-node config just takes extra keyword params — a gate is
`(ctx, **config) -> Directive`, and the node's `gate_args` supply the config
(bound for you). E.g. the built-in `rerun_on_signal(ctx, *, target)` used as
`gate="rerun_on_signal", gate_args={"target": "tech-stack"}`. Other registrable
kinds: a result→params export (`@registry.export`) and a custom run
(`@registry.run` + `NodeDef(run_ref="…")`) for a node that runs your own code
instead of an agent.

### Mocking agents for tests & dev

`mock_agent` applies the same idea to the agent itself: register a
deterministic, no-token stand-in by agent name, then run with `--mock-agents`
(or `mock_agents=True`) to route any node whose agent has one through it
instead of a real runtime — no subprocess, no LLM. It is a MODE, not a
runtime: a node without a registered mock still runs for real (partial
mocking).

```python
@registry.mock_agent("tech-stack-analyst")
def tech_stack_mock(inv, ctx):
    ctx.write_file("{run_dir}/tech-stack.md", "# Tech Stack\n\nPython, TypeScript.")
    return {"status": "ok", "result": {"languages": ["Python"]}}

run_flow(flow, registry=registry, product_key="acme", mock_agents=True)
```

`ctx` is a small, deterministic toolset: `write_file()`/`read_file()` accept the
same `{run_dir}`/`{param}` templating as a node's `inputs`, and `input()` reads
a structured work-order value — no prompt parsing, no LLM. See
[`docs/design/orchestrator/mock-agent.md`](docs/design/orchestrator/mock-agent.md).

**Orchestration backend.** `build_flow` compiles your graph into a runnable flow
callable that dispatches execution to the selected backend. The default
`InProcessBackend` runs in-process (threadpool + semaphore + stdlib logging, no
Prefect); the opt-in `PrefectBackend` (`build_flow(..., backend="prefect")`)
routes execution through [**Prefect**](https://www.prefect.io/) for parallel
fan-out, concurrency limits, and a run UI. The backend is a swappable seam — the
engine owns all flow logic and stays backend-free, so the backend can change
without touching your pipeline. See
[`docs/design/orchestrator/backend.md`](docs/design/orchestrator/backend.md).

## Install & run

Requires Python 3.14+, [`uv`](https://docs.astral.sh/uv/), and
[`task`](https://taskfile.dev/). For real runs, `opencode` must be on `PATH` and
configured with model access.

**Lean core, optional extras.** The default install is small — enough to declare
a pipeline and run it on the default in-process backend, with typed
params/results and config (pydantic, pydantic-settings, pyyaml, jsonschema,
python-dotenv). The heavy pieces are opt-in extras that match the runtime seams:

Installed from PyPI as `petrarca-agent-flow` (the import name is `agent_flow`).

| Install | Adds | Use when |
|---|---|---|
| `petrarca-agent-flow` | core only | programmatic `build_flow` on the in-process backend |
| `petrarca-agent-flow[cli]` | typer, rich | the `run_cli` command + live display |
| `petrarca-agent-flow[prefect]` | prefect | `--backend prefect` (run UI / scale) |
| `petrarca-agent-flow[all]` | cli + prefect | a full interactive install |
| `petrarca-agent-flow[dev]` | all + toolchain | development (implies `[all]`) |

```bash
pip install "petrarca-agent-flow[cli]"          # typical interactive use
pip install "petrarca-agent-flow[cli,prefect]"  # + the Prefect backend
task install                                    # editable dev install (implies [all])
```

Using a feature without its extra raises a clear message telling you which
extra to install (e.g. `run_cli` without `[cli]`, or `--backend prefect`
without `[prefect]`).

Then walk through your first pipeline, the two runnable examples (toy Tier-2 and
tech-assessment Tier-3, each with a token-free `--mock-agents` mode), the
`run_cli` flags/params, and writing agents that cooperate with agent-flow:
**[`docs/usage/index.md`](docs/usage/index.md)**.

> Run real OpenCode runs from a normal shell **outside** an OpenCode session (a
> nested OpenCode raises `UnknownError`).

## Develop

`task fct` is the local loop (format + lint + unit tests). The full task list
(`verify`, `test:all`, `test:opencode`, `build`, git hooks) and the coding
standards live in **[`CONTRIBUTING.md`](CONTRIBUTING.md)**.

## Layout

```
src/agent_flow/     the library
  core/             backend-free Tier-1 primitives (run_agent, control protocol,
                    result-schema, context ingestion, env)
  runners/          the agent-execution seam (AgentExecutor: Subprocess + InProcess + Mock)
                    and the subprocess wire adapters (AgentRunner) — OpenCode, …
  backends/         the graph-execution seam (FlowBackend) — inprocess (default), prefect (opt-in)
  cli/              run_cli + neutral event rendering + tables (the [cli] extra)
  engine, gates, node_builder, run_config, run_context, preflight, utils
                    the flow engine, flow-control gates, the one-call node, and
                    the run-time plumbing that ties the seams together
  flowdef/          the declarative FlowDef/NodeDef surface + compile_flow
examples/           imperative.py & declarative.py (Tier 3) + custom_flow.py (Tier 2)
docs/design/orchestrator/   the design (start at index.md)
```

Layer order: `utils < runners < core < engine/gates/node_builder < backends < cli`.

## Documentation

- **Using the library** (task-oriented) — install, write your first pipeline,
  write agents that work with agent-flow, and recipes for common tasks:
  [`docs/usage/index.md`](docs/usage/index.md).
- **Design** (the architecture and why) — problem, principles, the three tiers,
  and one focused document per concept (supervision, control-file, engine,
  gates, node_builder, input-plane, result-schema, backend, cli-events):
  [`docs/design/orchestrator/index.md`](docs/design/orchestrator/index.md).

## Contributing & License

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
workflow and conventions (and [`AGENTS.md`](AGENTS.md) if you use an AI coding
assistant). Licensed under the Apache License 2.0 — see [`LICENSE`](LICENSE).
