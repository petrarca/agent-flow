# examples

Runnable demonstrations of building pipelines on the `agent-flow` library. Each
example is a single file. The subprocess-based examples ship real opencode agents
in `.opencode/agent/` (`--runtime opencode`) AND register deterministic
`mock_agent` behaviours (see `mock_agents.py`), so they also run token-free with
`--mock-agents` (custom_flow, a Tier-2 flow, uses `--runtime mock` for its own
mock path). The in-process example needs neither.

The subprocess examples build the same shape:

    tech-stack -> tech-stack-verify -> ( domain(+verify) | architecture(+verify) ) -> summary

## imperative.py — author with `agent_node`

The Tier-3 pipeline built imperatively: `agent_node(...)` per node, wired with
`depends_on` / `parallel_group`, gates referenced by name (`require_file`,
`rerun_on_signal`). Runs via the reusable `run_cli`.

```bash
task example:imperative:mock PRODUCT=acme      # mock agents, no tokens
task example:imperative PRODUCT=acme           # real opencode + model
python -m examples.imperative flow nodes       # inspect the flow
```

## declarative.py — the same flow as a `FlowDef`

The identical pipeline authored as pure DATA: a `FlowDef` of `NodeDef`s
(serializable, no callables in the definition). Also shows **how to hook your
own logic**: a custom deciding gate and an observing `after_node` hook, both
registered on a `FlowRegistry` and referenced by name. The registry also carries
the `mock_agent` behaviours (via `mock_agents.register`), so `--mock-agents`
resolves a deterministic stand-in per agent name at compile time.

```bash
task example:declarative:mock PRODUCT=acme
python -m examples.declarative flow nodes
```

## custom_flow.py — Tier-2, your own flow

A hand-written flow that calls the `run_agent` primitive directly (analyst →
verifier → extractor), reusing `build_run_config` / `preflight` and demonstrating
transactions, rollback, and resume — the low-level escape hatch when you want to
own the flow shape yourself.

```bash
task example:custom:mock TOPIC="Hexagonal architecture"
task example:custom TOPIC="Hexagonal architecture"
```

## inprocess.py — in-process agents (no subprocess, no sidecar)

A different shape: a 2-node `classify -> respond` flow where both "agents" are
plain Python functions simulating PydanticAI-style agents (invocation in, typed
pydantic model out). They run via `InProcessExecutor` — no subprocess, no control
sidecar — while gates still read the typed object as `ctx.obj`, and `exports`
still flows a value (`category`/`urgency`) from one node to the next. Attached by
name (`NodeDef.impl_ref` + `registry.agent_impl`); the imperative form is
`agent_node(impl=fn)`.

It runs **programmatically via `run_flow`** (not `run_cli`): an all-in-process
flow has no runtime/agent-dir, and the CLI pre-flight is still subprocess-oriented
(issue #11). No `.opencode/` or `--runtime` needed.

```bash
task example:inprocess                                  # default ticket
task example:inprocess TICKET="cannot log in, urgent"   # your ticket
python -m examples.inprocess "minor typo on the about page"
```
