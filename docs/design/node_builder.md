---
type: Concept
title: Node builder — agent_node, the one-call node
description: The agent_node factory for the common 'run one agent' case, and the Tier-3 developer experience.
tags: [agent-flow, node_builder, agent_node, developer-experience, tier-3]
timestamp: 2026-07-23T07:51:35Z
---

# Node builder: `agent_node`

Tier 3's `Node` takes a `run` callable — maximally flexible, but it means a
consumer would hand-write prompt-building, control-path derivation, and the
executor call for every node. The overwhelmingly common shape is simply *"run one
agent, hand it a work order, point it at a control file, get the result."*
`agent_node(...)` builds exactly that node in one call.

It is a convenience, not a new layer: it returns a plain `Node`, so it mixes
freely with hand-written `run` callables in the same graph. It is the one module
that depends on BOTH the [engine](engine.md) (`Node`) and the execution seam
(`AgentInvocation` + `AgentExecutor`), keeping `engine/` itself decoupled from
the runtime.

## Signature

```python
agent_node(
    name, agent, *,
    inputs=None,             # KEY: value work order, templated (see input-plane.md)
    depends_on=(), parallel_group=None, criticality="blocking",
    instructions="",         # per-node instruction block, additive (see input-plane.md)
    context=(),              # per-node context SOURCES, content injected (see input-plane.md)
    gate=None,               # a bare callable gate you wrote (or gate_ref/gate_args by name)
    gate_ref=None, gate_args=None,   # a registered gate BY NAME + its config (see gates.md)
    result_schema=None,      # optional typed output (see result-schema.md)
    max_cycles=1,
    duration=None,           # PORTABLE name ("long"); run config maps it to seconds
    model=None,              # imperative escape hatch; usually set via run config
    agent_dir=None,          # imperative override (usually run config / probed)
    exports=None, export_ref=None,   # result->params publish hook (inline map or by name)
    impl=None,               # run IN-PROCESS: a callable (inv) -> result; no subprocess
) -> Node
```

`agent` is the runtime agent name (e.g. an opencode `--agent`). It is
**domain-neutral** — the library attaches no meaning to it.

`impl` selects HOW the agent runs (see "Executor selection"); it never changes
what `agent` means. There is no `mock_agent=` param and no `registry=` param: a
mock is resolved by agent name from the run's `FlowRegistry` under the
`--mock-agents` mode, and that registry reaches the node on its `RunContext`
(from `build_flow(registry=...)` / `run_cli(registry=...)`).

The registry is run-scoped, not node-scoped — one registry serves every node in
a flow, and no call site in the library or its examples has ever wanted two. It
arrives on the context so a node cannot consult a different registry than the
flow was built with; when it was a per-node argument, omitting it silently
disabled `--mock-agents` for that node and the real agent ran instead.

## No analyst/verifier concept

There is deliberately no notion of "analyst"/"verifier" in the library. A
verifier is just another `agent_node` that `depends_on` its subject and
carries a `gate_ref="rerun_on_signal"` [gate](gates.md) (with
`gate_args={"target": ...}`); the engine's bounded cross-node
[jump-back](engine.md) drives the re-run. Any node can route flow to any upstream
node — the library imposes no adjacency.

```python
nodes = [
    agent_node("tech-stack", "tech-stack-analyst",
               inputs={"REPORT": "{run_dir}/tech-stack.md"},
               gate_ref="require_file", gate_args={"path": "{run_dir}/tech-stack.md"}),
    agent_node("tech-stack-verify", "tech-stack-verifier",
               depends_on=("tech-stack",), criticality="degrade",
               gate_ref="rerun_on_signal", gate_args={"target": "tech-stack"}),
]
```

## What it does for you

The generated `run` is an `async def` closure (the engine `await`s it); its
single await point is the executor call below. Everything else — prompt
composition, small-file reads — is synchronous inline. Inside it, `agent_node`:

- resolves the `inputs` into a `KEY: value` work order via `resolve_work_order`,
  expanding `{name}` templates against the flow `params` (plus `{run_dir}`),
- composes the per-node prompt in order — per-node `context`, per-node
  `instructions`, the run-time per-node instruction (`--instruct` /
  `nodes.<n>.instructions`, via `ctx.node_overrides`), the ephemeral one-time
  instruction (from a gate's `Restart`/`GoTo`), then the work order (see
  [input-plane.md](input-plane.md)),
- builds a neutral `AgentInvocation` (prompt, run_dir, node, model, idle timeout,
  `result_schema`, run-wide `run_instructions`/`run_context`, and an
  `on_event` callback built from `RunContext.on_event_factory` under the NODE
  name),
- selects an executor and `await`s `executor.run(inv)` (the executor seam is
  async; an in-process `impl` may itself be sync or async — see below),
- returns the control envelope plus a little `_`-prefixed telemetry for the gate.

Note it does NOT call `run_agent`: it builds the invocation and hands it to an
`AgentExecutor`. The control-sidecar path is a subprocess concern (derived by
`SubprocessExecutor`); an in-process or mock node has no sidecar preamble.

## Executor selection

The generated `run` picks the executor — and the engine is blind to all of it:

1. **`--mock-agents` mode on AND** `registry` has a `mock_agent` for this node's
   `agent` → `MockExecutor` (deterministic, no tokens). This WINS over `impl`.
2. else `impl` set → `InProcessExecutor` (direct Python call, no subprocess).
3. else → `get_executor(runtime)` — a `SubprocessExecutor` for the selected
   runner (opencode, …).

Mock is a MODE, not a runtime; resolution is by AGENT name, so one
`registry.mock_agent(name)` registration covers every node running that agent.
See [mock-agent.md](mock-agent.md).

The Tier-3 payoff: a whole pipeline is declaration only. The tech example's
prior ~120 lines of hand-written glue (`_build_prompt` / `_control_path` /
`_invoke` / `_make_run` / `_make_gate`) collapse into `agent_node(...)` calls plus
ready gates.

## Where it lives

`src/agent_flow/node_builder/` (`agent_node`, `resolve_work_order`,
`build_work_order`, `control_path`).
