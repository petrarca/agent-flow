# agent-flow

Deterministic orchestration of coding-agent pipelines. `agent-flow` replaces the
fragile "LLM orchestrator agent" pattern with a **deterministic engine** that
supervises coding-agent subprocesses (opencode today) and runs them as a graph —
with parallelism, bounded re-runs, cross-node jump-back, telemetry, and optional
typed output. The execution backend (Prefect) and the agent runtime (opencode /
Claude Code / …) are both pluggable.

The control flow is **plain Python the engine executes** — no model sequences the
stages, so the orchestrator cannot hang or "lose the thread". Each agent runs as
an external process supervised by **liveness** (killed only when it goes silent,
never on a fixed wall-clock cap), and reports its outcome via a small JSON
**control sidecar**.

## Three usage tiers (high level → low level)

Pick the tier that fits; each is usable on its own. Higher tiers are more
declarative; lower tiers give more control.

```
TIER 3  DECLARATIVE      declare Nodes -> build_flow() -> a runnable Prefect flow
  (most declarative)     agent_node() = one call per agent            examples/tech_assessment
        │ composes
TIER 2  PRIMITIVES       call run_agent() as the leaf of YOUR OWN Prefect flow
        │ uses                                                        examples/toy_pipeline
TIER 1  ENGINE CORE      run_agent(): spawn + liveness-supervise + kill + sidecar verdict
  (closest to the metal) runner-agnostic; no Prefect
        │ invokes
        AGENT RUNTIME    opencode agents (.md) — external, unchanged
```

- **Tier 3 — declare the graph** (`agent_node` + `build_flow`): one call per
  agent; the library builds the prompt, sidecar path, and DAG.
- **Tier 2 — your own Prefect flow**: call `run_agent` as the leaf of a
  hand-written flow.
- **Tier 1 — one supervised agent** (`run_agent`): spawn + liveness-supervise +
  kill + read the sidecar verdict. Prefect-free.

**Orchestration backend.** Tier 3's `build_flow` compiles your graph into a
[**Prefect**](https://www.prefect.io/) flow (parallel fan-out, retries,
concurrency limits, a run UI). Prefect is a swappable seam — it is imported only
inside `build_flow`, and Tiers 1–2 do not require it — so the backend can change
without touching your pipeline. See
[`docs/design/orchestrator/backend.md`](docs/design/orchestrator/backend.md).

```python
from agent_flow import agent_node, build_flow
from agent_flow.gates import require_file, rerun_on_signal

nodes = [
    agent_node("tech-stack", "tech-stack-analyst",
               inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
               gate=require_file("tech-stack.md")),
    # a "verifier" is just another node that can jump the flow back:
    agent_node("tech-stack-verify", "tech-stack-verifier",
               depends_on=("tech-stack",), criticality="degrade",
               gate=rerun_on_signal(target="tech-stack")),
]
build_flow(nodes, name="tech")(product_key="acme", runtime="opencode")  # no run_dir -> temp dir under <temp>/agent-flow/
```

## Install

Requires Python 3.14+, [`uv`](https://docs.astral.sh/uv/), and
[`task`](https://taskfile.dev/). For real runs, `opencode` must be on `PATH` and
configured with model access.

```bash
task install            # editable install with dev tools
```

Optional extras: `agent-flow[pydantic]` (typed result output),
`agent-flow[cli]` (live-event view + rich tables via typer/rich).

## Run the examples

```bash
# Toy pipeline (Tier 2): analyst -> verifier -> extractor
task example:toy:mock TOPIC="Ports and Adapters"          # no tokens
task example:toy      TOPIC="Ports and Adapters" RUNTIME=opencode SHOW_EVENTS=--show-events

# Tech-assessment DAG (Tier 3): parallel analysts + verifier nodes
task example:tech:mock PRODUCT=my-product                 # no tokens
task example:tech      PRODUCT=my-product RUNTIME=opencode \
     SHOW_EVENTS=--show-events                            # real agents, live events
```

The tech example uses the library's reusable CLI (`agent_flow.run_cli`): generic
flags + arbitrary domain params via `-p/--param KEY=VALUE`, or a `--config`
YAML file. There is no built-in `--product` option — `--param` is the generic
protocol for all domain values:

```bash
uv run --with prefect python -m examples.tech_assessment.tech_flow \
  -p product_key=my-product -p repos_root=/tmp/repos --runtime opencode \
  -i "Experimental code-graph support is available; use it alongside RAG where sensible."
# or: --config run.yml   (settings + a params: section)
```

> Run real-opencode examples/e2e from a normal shell **outside** an opencode
> session (a nested opencode raises `UnknownError`).

## Develop

```bash
task install            # editable install with dev tools
task fct                # format + check (lint) + unit tests (the local loop)
task format             # ruff format
task check              # ruff check --fix
task verify             # read-only lint + format check (CI gate)
task test               # unit tests (fast, no subprocess)
task test:all           # unit + integration (mock subprocess; opencode e2e skipped)
task test:opencode      # opt-in real-opencode e2e (needs opencode + creds)
task build              # build sdist + wheel
task rebuild:all        # clean + install + format + check + test:all + build
task clean              # remove build artifacts and caches
```

Git hooks (fast checks on commit, deeper on push):

```bash
task pre-commit:install # install the pre-commit + pre-push hooks
task pre-commit:run     # run all hooks against the tree
```

Standards: ruff (`E,F,B,I,C90`, line length 150), `max-complexity = 10`, Python
3.14.

## Layout

```
src/agent_flow/          the library
  agent_runtime.py       run_agent — supervised subprocess + liveness + sidecar verdict
  runners.py             AgentRunner strategy (opencode / mock / …) + Event
  engine.py              Node / build_flow / plan_groups / interpret (DAG, re-runs, jump-back)
  batteries.py           agent_node — the one-call node
  gates.py               Directive / GateContext + ready gates
  control_protocol.py    the injected completion protocol (control-file contract)
  schema.py              result-schema seam (typed output; Pydantic optional)
  cli.py                 event projection + rich tables (cli extra)
  env.py / _prefect_env.py  .env loading; Prefect bootstrap (embedded/file/server)
examples/
  toy_pipeline/          Tier 2 demo (hand-written flow)
  tech_assessment/       Tier 3 demo (declared graph)
deploy/                  docker-compose (Prefect server + Postgres) for persistent mode
docs/design/orchestrator/  the design (start at index.md)
```

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
