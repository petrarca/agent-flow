# examples

Runnable demonstrations of building pipelines on the `agent-flow` library. Each
example is a single file; they share one simulated agent set in
`.opencode/agent/` (so every example runs with `--runtime mock` — no tokens — or
`--runtime opencode`).

All three build the same shape where relevant:

    tech-stack -> tech-stack-verify -> ( domain(+verify) | architecture(+verify) ) -> summary

## imperative.py — author with `agent_node`

The Tier-3 pipeline built imperatively: `agent_node(...)` per node, wired with
`depends_on` / `parallel_group`, gates referenced by name (`require_file`,
`rerun_on_signal`). Runs via the reusable `run_cli`.

```bash
task example:imperative:mock PRODUCT=acme      # mock agents, no tokens
task example:imperative PRODUCT=acme           # real opencode + model
python -m examples.imperative nodes list       # inspect the flow
```

## declarative.py — the same flow as a `FlowDef`

The identical pipeline authored as pure DATA: a `FlowDef` of `NodeDef`s
(serializable, no callables in the definition). Also shows **how to hook your
own logic**: a custom deciding gate and an observing `after_node` hook, both
registered on a `FlowRegistry` and referenced by name.

```bash
task example:declarative:mock PRODUCT=acme
python -m examples.declarative nodes list
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
