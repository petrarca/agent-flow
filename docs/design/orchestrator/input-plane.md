---
type: Concept
title: The input plane — how instructions reach an agent
description: The prompt channels (completion protocol, run-wide context/brief, per-node context/instructions, run-time instructions, work order) plus persona; templating; the CLI brief.
tags: [agent-flow, input-plane, instructions, prompt, templating, cli]
timestamp: 2026-07-23T07:51:35Z
---

# The input plane

Several channels feed an agent's prompt, each with a different owner and
lifetime. They compose in a **fixed order** into the final prompt:

```
[1 completion protocol]        library, ALWAYS      where to write status + the control JSON shape
[2 run-wide context]           consumer, at START   ingested FILE CONTENT for every agent (rules/standards)
[3 run-wide brief]             consumer, at START   inline text for every agent (the global directive)
[4 per-node context]           consumer, declared   ingested FILE CONTENT for this node only
[5 per-node instructions]      consumer, declared   inline text for this node only (build time)
[6 per-node RUN-TIME instr]    consumer, at RUN     inline text for this node only (CLI/config), additive LAST
[7 work order]                 consumer, declared   KEY: value, templated
```

Note the pattern: at each scope, **ingested context (files) precedes inline
instructions (text)** — the agent reads the authoritative rules first, then the
run-specific guidance, then the concrete task. "Context" is the fix for the
"agents don't read the rules you point them at" problem: the engine reads the
files and injects their *content*, so the rules are physically in the prompt,
not a reference to go fetch.

Plus a **separate** channel that is NOT part of this prompt:

- **Standing persona** lives in the agent `.md` (opencode loads it via
  `--agent <name>`). *What the agent is* is the agent's concern; the library
  never passes it. For runtimes without named agents (Claude Code), the persona
  is injected at call time via `AgentInvocation.instructions` /
  `--append-system-prompt` — the runner's job, not this composition.

## Where each comes from

- **(1) completion protocol** — injected by the library (see
  [control-file](control-file.md)); the consumer never writes it.
- **(2) run-wide context** — file SOURCES whose content is injected into every
  agent: `build_flow(shared_context=["{run_dir}/rules/security.md", …])`. The
  engine reads each file at run time and concatenates its content. This is how
  you guarantee an agent *has* the security rules / coding standards, rather
  than telling it to read them (the failure that motivated nested `AGENTS.md`).
- **(3) run-wide brief** — the global directive you pass when the run starts,
  including from the CLI: `--instructions/-i "…"` or `--instructions-file`. It
  reaches the library as the **typed** `build_flow(shared_instructions=…)`
  argument (or `run_agent(shared_instructions=…)` at Tier 1/2), threaded via
  `RunContext.shared_instructions`. It is deliberately a typed build-time value,
  **not** a `params` key — so it stays off the task-serialization path and out of
  the domain grab-bag (same precedent as [`on_event_factory`](cli-events.md)).
  Example: *"Experimental code-graph support is available; use it alongside RAG
  where it makes sense."*
- **(4) per-node context** — `agent_node(context=["…"])`, file SOURCES injected
  for one node only. Same "inject content, not a pointer" idea, scoped to a step.
- **(5) per-node instructions** — `agent_node(instructions="…")`, inline text,
  additive to the protocol/brief, for one node only. Set at BUILD time.
- **(6) per-node RUN-TIME instruction** — an extra instruction attached to a node
  at RUN time (not in `build_nodes()`): CLI `--instruct NODE="…"` (repeatable), a
  `node_instructions:` section in the `--config` YAML, or programmatically
  `build_flow(node_instructions={"node": "…"})`. It is appended **LAST** (after
  (5), before the work order), so it is the most recent standing guidance — which
  makes it an additive, last-word override ("ignore the prior instruction; do X
  instead"). CLI `--instruct` merges over the config section (CLI wins per node).
  May template run params. See [Per-node run-time instructions](#per-node-run-time-instructions).
- **(7) work order** — `agent_node(inputs={KEY: "value-or-{template}"})`, the
  per-run values (product key, report path, focus).

Context sources (2, 4) accept file paths or globs; a source matching no file is
warned about and skipped, never a crash. `run_agent` itself (Tier 1) takes the
already-read content string (`shared_context=...`); the batteries layer reads the
files for you.

## Templating

Instruction blocks (3, 5, 6), context sources (2, 4), and work-order values (7) may
reference run params via `{name}` (one-level `str.format`). `{run_dir}` is
provided automatically; other `{name}`s resolve from what you pass to
`pipeline(**params)` at start. A missing placeholder is left literal rather than
raising, so an optional param degrades gracefully.

```python
agent_node("tech-stack", "tech-stack-analyst",
           inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/tech-stack.md"},
           instructions="Focus on {product_key}; prefer a compact table.")
```

```bash
# run-wide brief from the CLI, injected into every agent:
python -m examples.tech_assessment.tech_flow -p product_key=acme --runtime opencode \
  -i "Experimental code-graph support is available; use it alongside RAG."
```

## Reserved param names

`agent_node`'s `run` callable reads a few well-known keys back out of `params`
(the same bag `{name}` templating draws from) to apply run-wide knobs per node:

- `runtime` — `"opencode"` | `"mock"` (which `AgentRunner` to use).
- `model` — provider/model string; empty/absent -> the runner omits `--model` so
  the runtime resolves it from its own config (never a hardcoded default).
- `idle_timeout_s` — liveness timeout in seconds (string in `params`, since
  `params` values are templating strings; coerced back to `int`).

`run_cli` injects `runtime`/`model`/`idle_timeout_s` into `params` from the
corresponding `RunConfig` settings (CLI flags / `AGENT_FLOW_*` env), so every
`agent_node` picks them up automatically. A per-node `agent_node(model=...,
idle_timeout_s=...)` always overrides the run-wide value.

**Consequence:** a domain `params_model` should avoid fields named `runtime`,
`model`, or `idle_timeout_s` unless it genuinely means the same thing — they are
effectively reserved names in the params bag.

## Run-context: params can also flow FROM a node

Params are not only set at start. They live in a run-scoped **run-context
service** (`run_context.py`) — a thread-safe store the engine installs from the
initial params. A node reads a *snapshot* of it when it starts (so it sees a
stable view for its execution), and a node can **publish** values into it for
downstream nodes via `agent_node(exports=...)`:

- declarative `{param_name: field}` — copy fields (attribute or dict key) into
  params;
- callable `(payload) -> Mapping` — full control.

The hook sees the VALIDATED typed object when the node set a `result_schema`, else
the raw result dict — one payload, no signature sniffing. The engine applies
exports after the node settles (Continue or cross-node GoTo), so a later node's
`{name}` templating picks them up automatically.
Example: the readiness check captures provenance and `exports` it, so every
downstream agent stamps the same `analysis_timestamp` / `pipeline_commit`
without re-capturing it.

Scope is same-process, **downstream-only** — exports target nodes that run
*after* the publisher, never parallel-group siblings (which may be serialized).
Scalar params are the first slice of a broader "dynamic prompt composition"
direction (injecting context/instructions at run time); see the roadmap.

A param that is *only ever* set this way (not a user input) is declared on the
`params_model` with a placeholder default and the `runtime_param()` marker
(`Field(json_schema_extra=runtime_param())`). The placeholder keeps `{name}`
templating resolvable from the first node;
`run_cli` recognises the marker and **omits the field from the resolved-params
summary** so it does not read as an input you could pass. The publishing node then
overwrites it. Example: `analysis_timestamp` / `pipeline_commit`.

## Per-node run-time instructions

Channel (6) lets you steer ONE node for a run without editing `build_nodes()`.
Three entry surfaces, all producing a `{node_name: text}` map:

- **CLI** — `--instruct NODE="text"` (repeatable, `NODE=text` like `-p`).
- **Config YAML** — a `node_instructions:` section (persist per-product steering):
  ```yaml
  node_instructions:
    analyst: "Weight the security assessment heavily for Dim 14."
    summary: "Keep it to one page; lead with the tenancy gap."
  ```
- **Programmatic** — `build_flow(node_instructions={"analyst": "…"})`.

CLI `--instruct` **merges over** the config section (CLI wins per node); the merged
map is threaded via `RunContext.node_instructions` and each batteries node appends
its own entry.

**Additive, LAST word.** The run-time instruction is appended AFTER the build-time
per-node instruction (5) and before the work order — the most recent standing
guidance the agent sees. So it overrides earlier instructions by recency, no
special flag needed: *"Ignore the prior instruction about compact tables — produce
the full breakdown instead."*

This is distinct from `start_from` (CLI `--start-from`, a per-invocation entry
point, never persisted in config), though the two pair naturally: when you re-enter
the flow at a node to iterate, you usually also want to tell that node what to do
differently.

## Mapping to the old orchestrator vocabulary

- "stuff currently in the orchestrator" → **(5)** per-node `instructions` and
  **(7)** `inputs`.
- "stuff passed when we start the orchestrator" → **(3)** the CLI/`build_flow`
  brief, and params interpolated into **(7)**.

## Where it lives

`src/agent_flow/core/control_protocol.py` (block 1), `core.agent_runtime.run_agent`
(composes 1 + 2 + the caller prompt), `batteries.agent_node` (composes 4 + 5 + 6
and forwards 2/3), the `RunContext.shared_instructions` / `RunContext.node_instructions`
/ `build_flow(node_instructions=)` plumbing in `engine.py`, and the CLI
`--instruct` + config `node_instructions:` handling in `cli/app.py` / `run_config.py`.
