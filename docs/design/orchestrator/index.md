---
type: Design Overview
title: agent-flow — deterministic orchestration of coding-agent pipelines
description: Overview and concept map for the agent-flow library design (OKF bundle index).
tags: [agent-flow, orchestration, design, overview, prefect, opencode]
timestamp: 2026-07-23T07:51:35Z
---

# agent-flow — design overview

`agent-flow` replaces the fragile "LLM orchestrator agent" pattern with a
**deterministic engine** that supervises coding-agent subprocesses (opencode
today) and runs them as a graph — with parallelism, bounded re-runs, cross-node
jump-back, telemetry, and optional typed output. The execution backend
(in-process local by default, Prefect opt-in) and the agent runtime (opencode /
Claude Code / …) are both pluggable.

This directory is an [OKF](https://github.com/GoogleCloudPlatform/knowledge-catalog/blob/main/okf/SPEC.md)
bundle: this `index.md` is the entry point; each concept lives in its own file
(see the map below).

## Problem

Multi-stage analysis pipelines are often driven by an **LLM orchestrator agent**
emitting `Task` calls to sequence specialists. Two structural problems:

1. **The orchestrator is an LLM** — it can hang, lose the thread past ~20 steps,
   forget rules in a long instruction file, and cannot resume after a crash (its
   state lives in a context window).
2. **The pipelines are ~90% identical machinery** — a readiness gate,
   analyst→verifier pairs with a re-run loop, some parallel groups, a reduce
   tail — expressed repeatedly in prose, with no shared code.

The fix: replace the LLM orchestrator with **deterministic code**, and factor the
shared machinery into **one library** a pipeline configures by declaring a graph.

## Scope — what the library is (and is not) for

The library does exactly two things:

1. **Instrument and control the execution** of an agent runtime: spawn it, pass
   it what it needs, supervise by liveness, kill on stale, capture the outcome,
   and run a graph of such agents.
2. **Expose optional hooks** (gates) so a *consumer* can inspect what an agent
   did and steer the flow.

Three concerns, kept strictly separate:

| Concern | Owner |
|---|---|
| Instrument + control execution | **the library** |
| What an agent *does* (read/write files, domain work, report content) | **the agent** (its `.md`) |
| Whether the side effects are acceptable + what to do next | **the consumer** (optional gates) |

The library reads exactly one thing back from an agent — the **control sidecar**
— and interprets only its `status` (verdict) and `rerun_required` (a flow
signal). Everything else the agent emits is opaque. See
[control-file](control-file.md).

## Design principles

1. **The orchestrator is deterministic code, not an LLM.** Sequencing,
   parallelism, retries, criticality, re-run caps are Python the engine executes
   — never rules an LLM must remember. ("Graph, not a loop.")
2. **Agents are unchanged.** Existing opencode `.md` agents keep their identity
   and are invoked as-is (`opencode run --agent <name>`).
3. **Lean core, optional extras.** The default install carries only what a
   programmatic `build_flow` run on the default in-process backend needs (pydantic,
   pydantic-settings, pyyaml, jsonschema, python-dotenv). The heavy pieces are
   opt-in extras matching the runtime seams: `[cli]` (typer + rich, the display
   layer) and `[prefect]` (the opt-in Prefect backend). Both are lazy-imported
   at their entry points, and using a feature without its extra raises a clear
   "install petrarca-agent-flow[...]" message.
4. **Swappable seams, shared everything else.** Three things may change behind
   thin adapters — the **execution backend** (in-process vs Prefect), the **agent
   runtime** (opencode vs Claude Code, via `AgentRunner`), and the **pipeline**
   itself (the declared graph). What is deliberately NOT abstracted is agent
   *content* (names, `.md` bodies, persona) — that is runtime-specific.
5. **Optional-everything ergonomics.** No gate = proceed; no schema = free-form
   result; no `--show-events` = quiet. You pay only for what you use.

## Architecture — three usage tiers

Each tier is usable on its own; the number reflects how close you are to the metal.

```
TIER 3  DECLARATIVE          declare Nodes -> build_flow() -> a runnable flow callable (dispatches to the backend)
        agent_node() = one call per agent (node builder)
              │ composes
TIER 2  PRIMITIVES           call run_agent() as the leaf of YOUR OWN Prefect flow
              │ uses
TIER 1  ENGINE CORE          run_agent(): spawn + liveness-supervise + kill + sidecar verdict
        AgentExecutor seam (Subprocess / InProcess / Mock); AgentRunner wire
        adapter (opencode / claude / …); gates; control protocol; schema seam
              │ invokes
        AGENT RUNTIME        opencode agents (.md) — external, unchanged
```

**Dependency direction is strictly downward** (verified): Tier 3's engine does
not import Tier 1 — they meet only through the caller-supplied `Node.run`
callable (`agent_node` is the one module that bridges both, on purpose).

A consumer picks a tier by need: Tier 1 to supervise one agent; Tier 2 to keep
full control of the flow; Tier 3 to declare the graph and let the library build
it. Pipelines differ only in their Tier-3 declaration.

## The 30-second example (Tier 3)

```python
from agent_flow import agent_node, build_flow

nodes = [
    agent_node("tech-stack", "tech-stack-analyst",
               inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
               gate_ref="require_file", gate_args={"relpath": "tech-stack.md"}),
    # a "verifier" is just ANOTHER node that can jump the flow back:
    agent_node("tech-stack-verify", "tech-stack-verifier",
               depends_on=("tech-stack",), criticality="degrade",
               gate_ref="rerun_on_signal", gate_args={"target": "tech-stack"}),
]
build_flow(nodes, name="tech")(product_key="acme", runtime="opencode")  # no run_dir -> temp dir under <temp>/agent-flow/
```

## Concept map

| Concept | Doc | What it covers |
|---|---|---|
| Supervision core | [supervision.md](supervision.md) | `run_agent`: liveness (not wall-clock), kill, sidecar verdict; the `AgentRunner` seam; why `--format json` |
| Control file | [control-file.md](control-file.md) | The sidecar contract: envelope (`status`/`agent`/`reason`/`rerun_required`) + opaque `result{}`; protocol injection |
| Engine | [engine.md](engine.md) | `Node`, `plan_groups`, `build_flow`; DAG + parallel groups; bounded re-runs; cross-node jump-back |
| FlowDef | [flowdef.md](flowdef.md) | the declarative surface: `FlowDef`/`NodeDef` (serializable data), the `FlowRegistry` (gates/exports/runs/schemas by name; `(ctx, **config)` gates), `compile_flow`/`run_flow` |
| Gates | [gates.md](gates.md) | `Directive` (Continue/Restart/GoTo/Stop), `GateContext`, ready-made gates — the consumer's flow-control hook |
| Node builder | [node_builder.md](node_builder.md) | `agent_node` — the one-call node; the Tier-3 developer experience |
| Input plane | [input-plane.md](input-plane.md) | The prompt channels (ingested context + inline instructions, global & per-node) + persona; templating; the CLI brief; run-context + `exports` (result->params) |
| Result schema | [result-schema.md](result-schema.md) | Typed agent output; Pydantic-optional; opt-in consumer convenience |
| Backend | [backend.md](backend.md) | `FlowBackend` seam; InProcessBackend (default, in-process) and PrefectBackend (opt-in); deployment modes |
| Mock agent | [mock-agent.md](mock-agent.md) | `mock_agent` — a deterministic stand-in for a real agent via the `--mock-agents` substitution MODE (not a runtime); `MockExecutor` (sibling `AgentExecutor`) + `MockAgentContext` tools; structured-interface simulation, no LLM |
| CLI & events | [cli-events.md](cli-events.md) | `Event`/`on_event`, `--show-events` projection, the Typer/rich CLI |

## Status

Implemented and tested: Tiers 1–3, gates + ready gates, node_builder,
control-file contract + protocol injection, result-schema seam, the
`AgentExecutor` seam (Subprocess / InProcess / Mock), the `--mock-agents`
substitution mode, live events + CLI, bounded re-runs and cross-node jump-back.
Runnable examples: `examples/custom_flow.py` (Tier 2), `examples/imperative.py`
and `examples/declarative.py` (Tier 3), and `examples/inprocess.py` (in-process
agents) — all green under `--mock-agents` and on real opencode.
