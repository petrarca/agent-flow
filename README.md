# Agent FLOW

Deterministic orchestration of coding-agent pipelines. `agent-flow` replaces the
fragile "LLM orchestrator agent" pattern — where a model is asked to sequence the
stages and inevitably hangs, loops, or "loses the thread" — with a **deterministic
engine** that runs your agents as a graph and supervises each one as an external
process.

## The problem it solves

Chaining coding agents (opencode, Claude Code, …) into a reliable pipeline runs
into four recurring problems. agent-flow addresses each directly:

1. **Deterministic orchestration.** The control flow is **plain Python the engine
   executes** — a real DAG (or State Mahcine) with dependencies, parallel fan-out, bounded re-runs,
   and cross-node jump-back. No model decides what runs next, so the orchestrator
   cannot hang or improvise the sequence. Given the same inputs, the same stages
   run in the same order.

2. **Reliable execution of agentic runners.** Each agent runs as a supervised
   subprocess with **liveness** timeouts (killed only when it goes *silent*, never
   on a fixed wall-clock cap — long thinking/writing phases are safe), clean
   process-group termination, bounded restarts, and a small JSON **control
   sidecar** the agent writes to report its real outcome (no prose-parsing). A
   crashed, stalled, or empty-output agent is detected and handled, not silently
   accepted.

3. **Controlled ingestion of context and runtime parameters.** A defined **input
   plane** composes each agent's prompt from ordered channels (completion
   protocol, run-wide context/brief, per-node context/instructions, templated work
   order) — the engine *injects file content*, so an agent physically has the
   rules rather than being told to go read them. Runtime parameters (model,
   liveness timeout, domain params) resolve through one precedence chain
   (CLI > env > .env > YAML > default) and flow through a **run-context service** —
   values can even be published by one node for downstream nodes (`exports`).

4. **Runner-agnostic.** The agent runtime (opencode today; Claude Code, Codex, …
   next) is a swappable `AgentRunner` strategy — only "how to build the command"
   and "how to parse the event stream" differ; supervision, the DAG, re-runs, the
   sidecar, and the display layer are written once and stay runtime-neutral. The
   execution backend is likewise a swappable seam: a Prefect-free **InProcessBackend**
   (default) or an opt-in **PrefectBackend** (`--backend prefect`).

## Feature shortlist

- **DAG engine** — `depends_on` dependencies, `parallel_group` fan-out, a
  fail-fast plan (cycles/unknown deps caught at build time).
- **Gates** — a post-node hook returns a directive: `Continue` / `Stop` /
  `Restart` / `GoTo`. Ready-made gates: `require_file`, `rerun_on_signal`,
  `rerun_on_named`.
- **Re-runs as jump-back** — a re-run rewinds to the named node and re-flows
  *forward* from there (re-running it + everything downstream), backward-only,
  bounded by `max_cycles`.
- **Start partway** — `--start-from NODE` (or a parallel-group) enters the flow
  at a chosen node, skipping upstream, to iterate on a late stage.
- **Run one node** — `--only NODE` (or a parallel-group) runs exactly that one
  node and stops (skips everything else); the surgical complement to
  `--start-from`. Mutually exclusive with it.
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
- **Runner strategy** — `AgentRunner` (opencode + a token-free `mock`; Claude
  Code stubbed), per-runner preflight checks and an `AgentRunnerInfo` doctor view
  (resolved model/tools per agent dir).
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
  primitives + DAG logic stay Prefect-free (import-isolation-guarded).
- **Three usage tiers** — from one supervised agent up to a declared graph (below).

## Three usage tiers (high level → low level)

Pick the tier that fits; each is usable on its own. Higher tiers are more
declarative; lower tiers give more control.

```
TIER 3  DECLARATIVE      a FlowDef (data) or agent_node() -> build_flow()      examples/declarative
  (most declarative)     a runnable flow; one node per agent                   examples/imperative
        │ composes
TIER 2  PRIMITIVES       call run_agent() as the leaf of YOUR OWN flow         examples/custom_flow
        │ uses
TIER 1  ENGINE CORE      run_agent(): spawn + liveness-supervise + kill + sidecar verdict
  (closest to the metal) runner-agnostic; backend-free
        │ invokes
        AGENT RUNTIME    opencode agents (.md) — external, unchanged
```

- **Tier 3 — declare the graph** (a `FlowDef`, or `agent_node` + `build_flow`):
  one node per agent; the library builds the prompt, sidecar path, and flow.
- **Tier 2 — your own flow**: call `run_agent` as the leaf of a
  hand-written flow.
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
        # Node 1 — run the "tech-stack-analyst" agent. `inputs` is the work order;
        # {product_key}/{run_dir} are filled from the run params at execution time.
        # The gate (a built-in, by name) asserts the agent actually wrote the
        # report — if not, the node retries (bounded).
        NodeDef(
            name="tech-stack",
            agent="tech-stack-analyst",
            inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
            gate="require_file",
            gate_args={"relpath": "tech-stack.md"},
        ),
        # Node 2 — a "verifier" is just another node. It runs after node 1, and its
        # gate can JUMP THE FLOW BACK: on a re-run signal the flow rewinds to
        # "tech-stack". criticality="degrade" means a failed check doesn't stop the run.
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

# Run it — one call. (Or hand `flow` to the reusable CLI: run_cli(flow), which
# also gives you `run` / `nodes list`.)
run_flow(flow, product_key="acme", runtime="opencode")
```

The same pipeline can be written imperatively with `agent_node(...)` (the
lower-level Tier-3 form) — see `examples/imperative.py` vs `examples/declarative.py`.

### Hooking your own logic

The built-in gates cover the common cases. To plug in **your own** logic, write a
function, register it on a `FlowRegistry`, and reference it from a node BY NAME —
the node stays pure data, your code lives in the registry:

```python
from agent_flow import FlowDef, NodeDef, FlowRegistry, run_flow
from agent_flow.gates import Continue, Stop

registry = FlowRegistry()  # built-in gates already seeded

@registry.gate("stack_usable")            # a custom DECIDING gate: (ctx) -> Directive
def stack_usable(ctx):
    if (ctx.result or {}).get("status") == "error":
        return Stop(reason="tech-stack could not be determined")
    return Continue()

@registry.on("after_node")                # an OBSERVING hook (telemetry; never steers flow)
def _log(node, outcome):
    print(f"{node.name}: {outcome.status} ({outcome.duration_s:.1f}s)")

flow = FlowDef(name="tech", nodes=[
    NodeDef(name="tech-stack", agent="tech-stack-analyst", gate="stack_usable"),  # referenced by name
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

| Install | Adds | Use when |
|---|---|---|
| `agent-flow` | core only | programmatic `build_flow` on the in-process backend |
| `agent-flow[cli]` | typer, rich | the `run_cli` command + live display |
| `agent-flow[prefect]` | prefect | `--backend prefect` (run UI / scale) |
| `agent-flow[all]` | cli + prefect | a full interactive install |
| `agent-flow[dev]` | all + toolchain | development (implies `[all]`) |

```bash
pip install "agent-flow[cli]"          # typical interactive use
pip install "agent-flow[cli,prefect]"  # + the Prefect backend
task install                           # editable dev install (implies [all])
```

Using a feature without its extra raises a clear message telling you which
extra to install (e.g. `run_cli` without `[cli]`, or `--backend prefect`
without `[prefect]`).

Then walk through your first pipeline, the two runnable examples (toy Tier-2 and
tech-assessment Tier-3, each with a token-free `mock` mode), the `run_cli`
flags/params, and writing agents that cooperate with agent-flow:
**[`docs/usage/index.md`](docs/usage/index.md)**.

> Run real-opencode runs from a normal shell **outside** an opencode session (a
> nested opencode raises `UnknownError`).

## Develop

`task fct` is the local loop (format + lint + unit tests). The full task list
(`verify`, `test:all`, `test:opencode`, `build`, git hooks) and the coding
standards live in **[`CONTRIBUTING.md`](CONTRIBUTING.md)**.

## Layout

```
src/agent_flow/     the library
  core/             backend-free Tier-1 primitives (run_agent, control protocol,
                    result-schema, context ingestion, env)
  runners/          the agent-runtime seam (AgentRunner) — opencode, mock, …
  backends/         the execution seam (FlowBackend) — inprocess (default), prefect (opt-in)
  cli/              run_cli + neutral event rendering + tables (the [cli] extra)
  engine, gates, batteries, run_config, run_context, preflight, utils
                    the flow engine, flow-control gates, the one-call node, and
                    the run-time plumbing that ties the seams together
  flowdef/          the declarative FlowDef/NodeDef surface + compile_flow
examples/           imperative.py & declarative.py (Tier 3) + custom_flow.py (Tier 2)
docs/design/orchestrator/   the design (start at index.md)
docs/design/orchestrator/   the design (start at index.md)
```

Layer order: `utils < runners < core < engine/gates/batteries < backends < cli`.

## Documentation

- **Using the library** (task-oriented) — install, write your first pipeline,
  write agents that work with agent-flow, and recipes for common tasks:
  [`docs/usage/index.md`](docs/usage/index.md).
- **Design** (the architecture and why) — problem, principles, the three tiers,
  and one focused document per concept (supervision, control-file, engine,
  gates, batteries, input-plane, result-schema, backend, cli-events):
  [`docs/design/orchestrator/index.md`](docs/design/orchestrator/index.md).

## Contributing & License

Contributions are welcome — see [`CONTRIBUTING.md`](CONTRIBUTING.md) for the
workflow and conventions (and [`AGENTS.md`](AGENTS.md) if you use an AI coding
assistant). Licensed under the Apache License 2.0 — see [`LICENSE`](LICENSE).
