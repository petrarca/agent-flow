# Agent Instructions

This file contains project-specific rules and conventions for AI coding
assistants working on **agent-flow**.

## Project Overview

agent-flow is a Python library for **deterministic orchestration of coding-agent
pipelines**. It replaces the fragile "LLM orchestrator agent" pattern with a
deterministic engine that supervises coding-agent subprocesses (opencode today)
and runs them as a graph — with parallelism, bounded re-runs, cross-node
jump-back, telemetry, and optional typed output. The execution backend (local
in-process default / opt-in Prefect) and the agent runtime (opencode / Claude
Code / …) are both pluggable.

### The three usage tiers (high level → low level)

- **Tier 3 — declarative**: declare `Node`s, call `build_flow` → a runnable flow
  callable that dispatches execution to the selected backend. `agent_node` builds
  a node that runs one agent in one call.
- **Tier 2 — primitives in your own flow**: call `run_agent` as the leaf of a
  hand-written flow (a consumer may hand-write a Prefect flow around it — the toy
  example does).
- **Tier 1 — engine core**: `run_agent` spawns + liveness-supervises + kills +
  reads the control sidecar. Runtime-agnostic; no Prefect.

The dependency direction is strictly downward: **Tier 3 (`engine.py`) must not
import Tier 1** (`core.agent_runtime`/`runners`) — they meet only through a
node's `run` callable. `batteries.py` is the single module allowed to bridge
both.

## Project Structure

```
src/agent_flow/
  __init__.py            # Public API exports (keep in sync with what ships)
  engine.py              # Node / RunContext / build_flow / plan_groups / interpret / _walk (Tier 3)
  batteries.py           # agent_node — one-call node (bridges engine + run_agent)
  gates.py               # Directive (Continue/Restart/GoTo/Stop) + GateContext + ready gates
  run_config.py          # RunConfig (pydantic-settings) / build_run_config / get_settings lifecycle
  run_context.py         # RunContextService — open domain params + exports
  preflight.py           # runtime pre-flight checks (opencode/agent_dir/prefect) -> Check results
  utils.py               # resolve_run_dir / default_temp_base / require_extra (pure top-level leaf)
  core/                  # Tier-1 primitives, GUARANTEED backend-free (run_agent,
                         #   control protocol, result-schema, context ingestion, env)
  runners/               # agent-runtime seam (AgentRunner Protocol) + get_runner registry
  backends/              # execution seam (FlowBackend ABC) + get_backend;
                         #   local default, prefect opt-in (DEFAULT_BACKEND="local")
  cli/                   # run_cli + display helpers (typer/rich, opt-in [cli] extra)
examples/
  toy_pipeline/          # Tier-2 demo (hand-written flow) + its .opencode/agent/*.md
  tech_assessment/       # Tier-3 demo (declared graph) + its .opencode/agent/*.md
docs/
  usage/                 # consumer OKF bundle (getting-started, writing-agents, recipes)
  design/orchestrator/   # design OKF bundle (one concept per file; start at index.md)
deploy/                  # docker-compose (Prefect server + Postgres) for persistent mode
```

## Running commands (full output)

When running a command whose output matters, capture and read the FULL output —
do not truncate with `tail`, `head`, `grep`, or similar. Truncation hides errors,
stack traces, and warnings that appear outside the visible window. Prefer piping
through `tee` to a file and reading the whole file:

```bash
some-command 2>&1 | tee /tmp/out.log   # then read /tmp/out.log in full
```

This is especially important for `task fct`, the examples, and any run that can
fail partway — the interesting line is rarely the last one.

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
- Dependencies are a lean core plus opt-in extras. Core (always installed):
  pydantic, pydantic-settings, pyyaml, jsonschema, python-dotenv — enough to
  declare a pipeline and run it on the default LocalBackend. The heavy pieces are
  extras matching the runtime seams: `[prefect]` (the opt-in PrefectBackend) and
  `[cli]` (typer + rich for `run_cli` / display); `[all]` is both, `[dev]` adds
  the tooling. An optional dep is lazy-imported at its entry point only — prefect
  inside `backends/prefect.py`, rich/typer inside `cli/` — guarded by
  `utils.require_extra(...)` so a missing extra fails with an actionable
  "install agent-flow[...]" message.
- When adding a public symbol, export it in `src/agent_flow/__init__.py` and keep
  the module-docstring "Public API" example current.

## Git Conventions

- Commit prefixes: `feat:`, `fix:`, `refactor:`, `chore:`, `docs:`, `test:`.
- Short messages, max ~two sentences per point.
- **Run `task fct` (format + check + test) before every commit** and make sure it
  passes. The pre-commit hook enforces this, but run it yourself first — never
  commit code that has not passed `task fct`.
- **Never commit or push implicitly.** Do not run any git write operation
  (`commit`, `push`, `merge`, `rebase`, `tag`, `reset --hard`, etc.) unless the
  user has explicitly instructed it for that specific action. Do not chain a
  commit/push onto another task on your own initiative. When in doubt, stop and
  ask. Read-only git (`status`, `log`, `diff`) is fine without asking.
- `main` is the default branch. Do not commit `work*`, `.venv`, `.env`, caches,
  or `node_modules` (all gitignored).

## Documentation

Docs are OKF bundles (`docs/usage/`, `docs/design/orchestrator/`) — every file
has a `type` frontmatter field. When you change behavior, update the matching
concept doc and verify code snippets run. The README covers install + the three
tiers + running the examples.
