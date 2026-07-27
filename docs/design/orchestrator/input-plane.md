---
type: Concept
title: The input plane — how instructions reach an agent
description: The prompt channels named by scope x kind (completion protocol, run context/instructions, node context/instructions, run-time and attempt instructions, work order) plus persona; the two rendering seams; templating; the CLI brief.
tags: [agent-flow, input-plane, instructions, prompt, templating, cli, prompt-parts]
timestamp: 2026-07-23T07:51:35Z
---

# The input plane

Several channels feed an agent's prompt, each with a different owner and
lifetime. They compose in a **fixed order** into the final prompt:

```
[1 completion protocol]        library, ALWAYS      where to write status + the control JSON shape
[2 run context]                consumer, at START   ingested FILE CONTENT for every agent (rules/standards)
[3 run instructions]           consumer, at START   inline text for every agent (the global brief)
[4 node context]               consumer, declared   ingested FILE CONTENT for this node only
[5 node instructions]          consumer, declared   inline text for this node only (build time)
[6 node RUN-TIME instructions] consumer, at RUN     inline text for this node only (CLI/config), additive LAST
[7 attempt instruction]        gate, per ATTEMPT    one-time text from Restart/GoTo, verbatim, ephemeral
[8 work order]                 consumer, declared   the node's input DATA, templated
```

## Naming: scope × kind

Every channel is one cell of a small grid, which is why the order above is
self-explanatory rather than arbitrary:

- **scope** — how far it reaches and how long it lives: **run** → **node** →
  **attempt**.
- **kind** — what it is: **context** (ingested FILE CONTENT), **instructions**
  (inline text), **data** (the work order).

| # | scope | kind | name | code |
|---|---|---|---|---|
| 1 | library | protocol | completion protocol | `build_verdict_preamble` (runner) |
| 2 | run | context | run context | `run_context` |
| 3 | run | instructions | run instructions | `run_instructions` |
| 4 | node | context | node context | `context` |
| 5 | node | instructions | node instructions | `instructions` |
| 6 | node (at run time) | instructions | node run-time instructions | `node_instructions` |
| 7 | attempt | instructions | attempt instruction | `one_time_instruction` |
| 8 | node | data | work order | `inputs` |

The same word means the same thing at every scope — `run_instructions` and a
node's `instructions` are both inline guidance, differing only in reach. That is
why the CLI keeps `--instructions` (run) beside `--instruct NODE=…` (node): one
concept, two scopes.

Broadest scope first, and within a scope **context precedes instructions
precedes data** — the agent reads the authoritative rules, then the guidance,
then the concrete task.

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
- **(2) run context** — file SOURCES whose content is injected into every
  agent: `build_flow(run_context=["{run_dir}/rules/security.md", …])`. The
  engine reads each file at run time and concatenates its content. This is how
  you guarantee an agent *has* the security rules / coding standards, rather
  than telling it to read them (the failure that motivated nested `AGENTS.md`).
- **(3) run instructions** — the global brief you pass when the run starts,
  including from the CLI: `--instructions/-i "…"` or `--instructions-file`. It
  reaches the library as the **typed** `build_flow(run_instructions=…)`
  argument (or `run_agent(run_instructions=…)` at Tier 1/2), threaded via
  `RunContext.run_instructions`. It is deliberately a typed build-time value,
  **not** a `params` key — so it stays off the task-serialization path and out of
  the domain grab-bag (same precedent as [`on_event_factory`](cli-events.md)).
  Example: *"Follow the team's coding standards and cite a source for every
  finding."*
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
- **(7) attempt instruction** — one-time text a gate attaches to `Restart`/`GoTo`
  (see [gates](gates.md)). Rendered VERBATIM, with no library heading — the gate
  owns its framing — and cleared after that attempt.
- **(8) work order** — `agent_node(inputs={KEY: "value-or-{template}"})`, the
  per-run values (product key, report path, focus).

Context sources (2, 4) accept file paths or globs; a source matching no file is
warned about and skipped, never a crash. `run_agent` itself (Tier 1) takes the
already-read content string (`run_context=...`); the node-builder layer reads the
files for you.

## Templating

Instruction blocks (3, 5, 6, 7), context sources (2, 4), and work-order values (8) may
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
python -m examples.declarative run -p product_key=acme --runtime opencode \
  -i "Follow the team's coding standards and cite a source for every finding."
```

## Reserved param names

`agent_node`'s `run` callable reads a few well-known keys back out of `params`
(the same bag `{name}` templating draws from) to apply run-wide knobs per node:

- `runtime` — the out-of-process runner to use, e.g. `"opencode"` (a real
  `AgentRunner`; Claude Code is stubbed). Mock is NOT a runtime value — see
  `mock_agents` below.
- `mock_agents` — the `--mock-agents` substitution MODE (bool): route any node
  whose agent has a registered `mock_agent` through `MockExecutor` instead of its
  runner. Nodes without one still run for real (partial mocking).
- `model` — provider/model string; empty/absent -> the runner omits `--model` so
  the runtime resolves it from its own config (never a hardcoded default).
- `idle_timeout_s` — liveness timeout in seconds (string in `params`, since
  `params` values are templating strings; coerced back to `int`).

`run_cli` injects `runtime`/`mock_agents`/`model`/`idle_timeout_s` into `params`
from the corresponding `RunConfig` settings (CLI flags / `AGENT_FLOW_*` env), so
every `agent_node` picks them up automatically. A per-node `agent_node(model=...,
idle_timeout_s=...)` always overrides the run-wide value.

**Consequence:** a domain `params_model` should avoid fields named `runtime`,
`mock_agents`, `model`, or `idle_timeout_s` unless it genuinely means the same
thing — they are effectively reserved names in the params bag.

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
map is threaded via `RunContext.node_instructions` and each agent-node appends
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
  **(8)** `inputs`.
- "stuff passed when we start the orchestrator" → **(3)** the CLI/`build_flow`
  brief, and params interpolated into **(8)**.

## Rendering: two seams, one invariant

Channels 2–8 reach a renderer as **parts**, never pre-joined (`PromptParts`).
Two seams compose as a pipeline, so overriding either — or both — is well
defined:

```
resolved inputs (dict)
      │  registry.work_order(fn)     INNER: data -> text   (restyle the work order)
      ▼
parts.work_order (str) + the other channels
      │  registry.prompt(fn)         OUTER: parts -> body  (control the whole layout)
      ▼
prompt body
      │  runner.build_verdict_preamble   prepended by the executor
      ▼
the agent's prompt
```

Defaults: `render_prompt` (markdown headings, the order above) and
`render_work_order_xml` (`<KEY>value</KEY>`; `render_work_order_lines` ships for
the pre-0.3 `KEY: value` shape). `PromptParts` also carries `inputs`, the work
order still structured, so an outer renderer may lay the data out itself rather
than consume the inner one's text.

**The invariant: channel 1 is not renderable.** The completion protocol is half
of the verdict contract — the executor injects a sidecar path and then reads back
that exact path — so a prompt renderer cannot touch it. A runner may *replace* it
(`build_verdict_preamble`) precisely because a runner owns **both** halves and
keeps them in step; a structured-output runner would swap the instruction and the
harvest together.

## Where it lives

`src/agent_flow/core/control_protocol.py` (block 1, the control preamble —
prepended by `SubprocessExecutor` in `core/agent_runtime.py`, subprocess-only).
`runners/base.py` holds `PromptParts` and the default `render_prompt`;
`node_builder.agent_node` fills the parts (reading context files into content,
templating, rendering the work order) and calls the registered renderer.
`compose_prompt(inv)` remains for a Tier-1/2 caller assembling a prompt from an
invocation directly. The `RunContext.run_instructions` / `run_context` /
`node_instructions` / `one_time_instruction` plumbing lives in `engine.py`
(`build_flow`); the CLI `--instruct` + config `node_instructions:` handling lives
in `cli/commands/run.py`, with the `node_instructions` field on `run_config.py`.
A FlowDef's `run_context` is threaded onto `RunCliContext` by `cli/app.py` — it
has no CLI flag (it is a pipeline declaration, not a per-run knob), so without
that wire `run_cli` would silently drop it while `run_flow` honoured it.
