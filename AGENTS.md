# Agent Instructions

This file contains project-specific rules and conventions for AI coding
assistants working on **agent-flow**.

## Project Overview

agent-flow is a Python library for **deterministic orchestration of coding-agent
pipelines**. It replaces the fragile "LLM orchestrator agent" pattern with a
deterministic engine that supervises coding-agent subprocesses (opencode today)
and runs them as a graph — with parallelism, bounded re-runs, cross-node
jump-back, telemetry, and optional typed output. The execution backend (Prefect)
and the agent runtime (opencode / Claude Code / …) are both pluggable.

### The three usage tiers (high level → low level)

- **Tier 3 — declarative**: declare `Node`s, call `build_flow` → a runnable
  Prefect flow. `agent_node` builds a node that runs one agent in one call.
- **Tier 2 — primitives in your own flow**: call `run_agent` as the leaf of a
  hand-written Prefect flow.
- **Tier 1 — engine core**: `run_agent` spawns + liveness-supervises + kills +
  reads the control sidecar. Runtime-agnostic; no Prefect.

The dependency direction is strictly downward: **Tier 3 (`engine.py`) must not
import Tier 1** (`agent_runtime`/`runners`) — they meet only through a node's
`run` callable. `batteries.py` is the single module allowed to bridge both.

## Project Structure

```
src/agent_flow/
  __init__.py            # Public API exports (keep in sync with what ships)
  agent_runtime.py       # run_agent — supervised subprocess + liveness + sidecar verdict (Tier 1)
  runners.py             # AgentRunner strategy (opencode / mock / claude stub) + Event
  engine.py              # Node / RunContext / build_flow / plan_groups / interpret / _walk (Tier 3)
  batteries.py           # agent_node — one-call node (bridges engine + run_agent)
  gates.py               # Directive (Continue/Restart/GoTo/Stop) + GateContext + ready gates
  control_protocol.py    # build_control_preamble — the injected control-file contract
  schema.py              # ResultSchema protocol / JsonSchema / ValidationOutcome / coerce_schema
  schema_pydantic.py     # PydanticSchema adapter (optional `pydantic` extra)
  context.py             # read_context_blocks — inject rules/standards file CONTENT into prompts
  run_config.py          # RunConfig / load_run_config / parse_params (the CLI config protocol)
  cli.py                 # run_cli + event_printer + print_results_table (optional `cli` extra)
  utils.py               # resolve_run_dir / default_temp_base
  env.py                 # load_env (.env -> os.environ)
  _prefect_env.py        # bootstrap (embedded / file / server Prefect modes)
  _mock_agent.py         # no-token stand-in for `opencode run` (same sidecar contract)
examples/
  toy_pipeline/          # Tier-2 demo (hand-written flow) + its .opencode/agent/*.md
  tech_assessment/       # Tier-3 demo (declared graph) + its .opencode/agent/*.md
docs/
  usage/                 # consumer OKF bundle (getting-started, writing-agents, recipes)
  design/orchestrator/   # design OKF bundle (one concept per file; start at index.md)
deploy/                  # docker-compose (Prefect server + Postgres) for persistent mode
```

## Build & Test

House-standard task names (same across petrarca repos):

```bash
task setup         # create the venv
task install       # editable install with dev tools
task fct           # format + check (lint) + test — run before committing
task format        # ruff format
task check         # ruff check --fix
task verify        # CI gate: lint + format check, read-only
task test          # unit tests (fast)
task test:unit     # unit tests only
task test:integration  # integration (mock subprocess)
task test:all      # unit + integration
task test:opencode # opt-in real-opencode e2e (needs opencode; run OUTSIDE an opencode session)
task build         # build sdist + wheel
task rebuild:all   # clean + install + format + check + test:all + build
task clean         # remove build artifacts and caches
task pre-commit:install   # install git pre-commit + pre-push hooks
task pre-commit:update    # update pinned hook versions
task pre-commit:run       # run all hooks against the tree
```

Run the examples:

```bash
task example:toy:mock  TOPIC="Ports and Adapters"
task example:tech:mock PRODUCT=my-product
task example:tech      PRODUCT=my-product RUNTIME=opencode SHOW_EVENTS=--show-events
```

> Real-opencode runs must start from a normal shell **outside** an opencode
> session (a nested opencode raises `UnknownError`).

## Core Concepts (do not violate)

### The control-file contract

An agent signals completion by writing a JSON **control sidecar** — the SOLE
verdict. The library injects the protocol into the prompt
(`build_control_preamble`); agent `.md` files carry only domain instructions.

- **Envelope (engine reads):** `status` (`ok`/`verified`/`error`), `agent`,
  `reason`, and `rerun_required` (a flow-control signal a gate consumes).
- **Payload (only gates/consumers read):** `result` — free-form dict. The engine
  never looks inside.
- **No `artifact` field** — what an agent produces is expressed in the files it
  was told to write, not reported back.
- **No sidecar → error.** The engine never inspects artifacts to guess success.

### Two directories (never conflate)

- **`run_dir`** — where control sidecars + relative artifact paths resolve. NOT a
  cwd. Unset → a fresh temp dir under `<temp>/agent-flow/` (ephemeral; pass an
  explicit `run_dir` for output you keep). Resolved once per run via
  `utils.resolve_run_dir`.
- **`agent_dir`** — where agent DEFINITIONS live (opencode `--dir`); becomes the
  subprocess cwd. Global default via `build_flow(agent_dir=)`, per-node override
  via `agent_node(agent_dir=)`.

### Gates are the consumer's optional hook

A gate `(GateContext) -> Directive` decides flow control AFTER an agent runs. The
engine never auto-fails on schema/artifact issues — a gate does. A verifier is
NOT a library concept: it is just another node that `depends_on` its subject and
returns `GoTo(subject)`.

### Typed output

`result_schema` is an opt-in convenience. `ctx.result["result"]` is always the
dict (validated if a schema was attached). `ctx.result["_result_obj"]` is a
Pydantic model instance ONLY when a `PydanticSchema` was used, else `None` (a
dict schema / no schema add no new object).

### The input plane (prompt composition order)

`[control protocol] [run-wide context] [run-wide brief] [per-node context]
[per-node instructions] [work order]`. "Context" = FILE CONTENT ingested by the
library (the fix for "agents don't read the rules"); "instructions" = inline
text. Any `pipeline(**params)` key is a `{name}` template usable in
inputs/context/paths. To hand a value TO the agent, put it in `inputs`.

## Code Quality

- Ruff `E, F, B, I, C90`, line length **150**, McCabe max-complexity **10**.
- No bare `except Exception`; catch concrete types. `except A, B:` (no `as`) is
  valid Python 3.14 (PEP 758) — do not flag it.
- Keep the core dependency-light: Prefect lazy-imported inside `build_flow`;
  `pydantic`/`rich`/`typer`/`jsonschema`/`pyyaml` are optional extras, never
  imported at core import time.
- When adding a public symbol, export it in `src/agent_flow/__init__.py` and keep
  the module-docstring "Public API" example current.

## Git Conventions

- Commit prefixes: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`.
- Short messages, max ~two sentences per point.
- Never commit or push automatically — wait for explicit instruction. Read-only
  git (`status`, `log`, `diff`) is fine.
- `main` is the default branch. Do not commit `work*`, `.venv`, `.env`, caches,
  or `node_modules` (all gitignored).

## Documentation

Docs are OKF bundles (`docs/usage/`, `docs/design/orchestrator/`) — every file
has a `type` frontmatter field. When you change behavior, update the matching
concept doc and verify code snippets run. The README covers install + the three
tiers + running the examples.
