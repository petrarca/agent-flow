---
type: Concept
title: The input plane — how instructions reach an agent
description: The nine prompt channels named by scope x kind (completion protocol, run context/instructions, run additional instructions, node context/instructions, node run-time instructions, attempt instruction, work order) plus persona; the two rendering seams; templating; the additive CLI brief.
tags: [agent-flow, input-plane, instructions, prompt, templating, cli, prompt-parts]
timestamp: 2026-07-27T00:00:00Z
---

# The input plane

Several channels feed an agent's prompt, each with a different owner and
lifetime. They compose in a **fixed order** into the final prompt:

```
[1 completion protocol]         library, ALWAYS      where to write status + the control JSON shape
[2 run context]                 flow, DECLARED       ingested FILE CONTENT for every agent (rules/standards)
[3 run instructions]            flow, DECLARED       inline STANDING brief for every agent
[4 run ADDITIONAL instructions] run config, at RUN   inline text for every agent this run (-i/--instructions), APPENDS to [3]
[5 node context]                flow, DECLARED       ingested FILE CONTENT for this node only
[6 node instructions]           flow, DECLARED       inline text for this node only (build time)
[7 node RUN-TIME instructions]  run config, at RUN   inline text for this node only (--instruct/nodes.<n>), additive LAST
[8 attempt instruction]         gate, per ATTEMPT    one-time text from Restart/GoTo, verbatim, ephemeral
[9 work order]                  flow, DECLARED       the node's input DATA, templated
```

Note the symmetry: at BOTH the run scope and the node scope, a **standing**
channel (declared on the flow) is followed by an **additional** channel supplied
at run time from the run config. `-i`/`--instructions` is the run-scope one;
`--instruct NODE=…` is the node-scope one. Neither replaces its standing
counterpart — both APPEND.

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
| 2 | run | context | run context | `FlowDef.run_context` |
| 3 | run | instructions (standing) | run instructions | `FlowDef.run_instructions` |
| 4 | run (at run time) | instructions (additional) | run additional instructions | `RunConfig.instructions` (`-i`) → `run_additional_instructions` |
| 5 | node | context | node context | `NodeDef.context` |
| 6 | node | instructions (standing) | node instructions | `NodeDef.instructions` |
| 7 | node (at run time) | instructions (additional) | node run-time instructions | `RunConfig.nodes.<n>.instructions` (`--instruct`) → `node_runtime_instructions` |
| 8 | attempt | instructions | attempt instruction | `one_time_instruction` |
| 9 | node | data | work order | `inputs` |

The same word means the same thing at every scope — `run_instructions` and a
node's `instructions` are both inline STANDING guidance, differing only in reach.
And each scope has a matching **additional** channel supplied at run time that
APPENDS to the standing one: that is why the CLI keeps `--instructions`/`-i`
(run scope) beside `--instruct NODE=…` (node scope) — one pattern, two scopes.

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
  agent, declared on the flow: `FlowDef(run_context=["{run_dir}/rules/security.md", …])`
  (or `build_flow(run_context=…)` in the imperative form). The engine reads each
  file at run time and concatenates its content. This is how you guarantee an
  agent *has* the security rules / coding standards, rather than telling it to
  read them (the failure that motivated nested `AGENTS.md`).
- **(3) run instructions** — the flow's STANDING global brief, declared on the
  flow: `FlowDef(run_instructions="…")` (or `build_flow(run_instructions=…)`),
  threaded via `RunContext.run_instructions`. A typed build-time value, **not** a
  `params` key. Example: *"Follow the team's coding standards and cite a source
  for every finding."*
- **(4) run ADDITIONAL instructions** — this run's extra run-wide brief, supplied
  at RUN time and APPENDED to (3): the CLI `--instructions/-i "…"` (or
  `--instructions-file`), the run config `instructions:` key, or
  programmatically `run_cli(..., run_config={"instructions": "…"})` /
  `run_flow(..., run_config={"instructions": "…"})`. Threaded via
  `RunContext.run_additional_instructions`. It does **not** replace the flow's
  standing brief — both render (fixing a 0.3.0 bug where the flow's brief was
  dropped under `run_cli`). A flow that declares no `run_instructions` behaves
  exactly as before: (4) is then the only run-wide brief.
- **(5) per-node context** — `agent_node(context=["…"])` / `NodeDef(context=…)`,
  file SOURCES injected for one node only. Same "inject content, not a pointer"
  idea, scoped to a step.
- **(6) per-node instructions** — `agent_node(instructions="…")` /
  `NodeDef(instructions=…)`, inline STANDING text for one node only. Set at BUILD
  time.
- **(7) per-node RUN-TIME instruction** — an extra instruction attached to a node
  at RUN time (not in the flow): CLI `--instruct NODE="…"` (repeatable), a
  `nodes.<node>.instructions` entry in the `--config`, or programmatically
  `run_config={"nodes": {"node": {"instructions": "…"}}}`. It is appended **LAST**
  (after (6), before the work order), so it is the most recent standing guidance —
  an additive, last-word override ("ignore the prior instruction; do X instead").
  CLI `--instruct` wins over a config `nodes:` entry per node. May template run
  params. See [Per-node run-time instructions](#per-node-run-time-instructions).
- **(8) attempt instruction** — one-time text a gate attaches to `Restart`/`GoTo`
  (see [gates](gates.md)). Rendered VERBATIM, with no library heading — the gate
  owns its framing — and cleared after that attempt.
- **(9) work order** — `agent_node(inputs={KEY: "value-or-{template}"})`, the
  per-run values (product key, report path, focus).

Context sources (2, 5) accept file paths or globs; a source matching no file is
warned about and skipped, never a crash. `run_agent` itself (Tier 1) takes the
already-read content string (`run_context=...`); the node-builder layer reads the
files for you.

## Templating

Instruction blocks (3, 4, 6, 7, 8), context sources (2, 5), and work-order values (9) may
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
- `idle_timeout_s` — the run-wide liveness timeout in seconds (string in
  `params`; coerced back to `int`). It is the fallback for a node that declares
  no `duration`; see [supervision](supervision.md) and the duration vocabulary
  below.

`run_cli` (and `run_flow`) inject `runtime`/`mock_agents`/`model`/`idle_timeout_s`
into `params` from the corresponding `RunConfig` settings, so every `agent_node`
picks them up automatically.

Per-node overrides no longer live on the portable node. A node declares only a
PORTABLE `duration` name (`NodeDef(duration="long")`); the run config's
`durations: {long: 900}` maps it to seconds. Concrete per-node values — `model`,
`agent_dir`, a raw `idle_timeout_s`, an extra `instructions`, `options` — live in
`RunConfig.nodes.<node>` (the shadow of the node, keyed by name). Precedence per
setting: `nodes.<n>` value > the imperative `agent_node(...)` arg > the run-wide
value > default.

**Consequence:** a domain `params_model` should avoid fields named `runtime`,
`mock_agents`, `model`, or `idle_timeout_s` unless it genuinely means the same
thing — they are effectively reserved names in the params bag.

## Run-context: params can also flow FROM a node

Params are not only set at start. They live in a run-scoped **run-context
service** (`run_context.py`) — a thread-safe store the engine installs from the
initial params, held in a **ContextVar** so it is scoped to the RUN's async task
tree, not the process. Two flows running concurrently in one process (an async
server handling two requests) each get their own store, so neither reads nor
overwrites the other's params. A node reads a *snapshot* of it when it starts (so it sees a
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

Scope is same-process and **downstream-only** — exports target nodes that run
*after* the publisher, never parallel-group siblings (which may be serialized).
"Same-process" means the store is not distributed: nodes run as tasks in one
process and share the run's store; it is per-RUN, not per-process, so concurrent
runs stay isolated.
Scalar params are the first slice of a broader "dynamic prompt composition"
direction (injecting context/instructions at run time); see the roadmap.

A param that is *only ever* set this way (not a user input) is declared on the
flow's params model (`FlowDef(params_schema=...)`, or an imperative
`params_model=`) with a placeholder default and the `runtime_param()` marker
(`Field(json_schema_extra=runtime_param())`). The placeholder keeps `{name}`
templating resolvable from the first node;
`run_cli` recognises the marker and **omits the field from the resolved-params
summary** so it does not read as an input you could pass. The publishing node then
overwrites it. Example: `analysis_timestamp` / `pipeline_commit`.

## Per-node run-time instructions

Channel (7) lets you steer ONE node for a run without editing the flow. It is one
facet of the per-node run config `nodes.<node>` (which also carries `model`,
`agent_dir`, `duration`, `idle_timeout_s`, `options`). Three entry surfaces:

- **CLI** — `--instruct NODE="text"` (repeatable, `NODE=text` like `-p`); populates
  `nodes.<NODE>.instructions`.
- **Config** — a `nodes:` section (persist per-product steering), in a `--config`
  file or inline JSON:
  ```yaml
  nodes:
    analyst: {instructions: "Weight the security assessment heavily for Dim 14."}
    summary: {instructions: "Keep it to one page; lead with the tenancy gap."}
  ```
- **Programmatic** — `run_config={"nodes": {"analyst": {"instructions": "…"}}}`.

CLI `--instruct` **wins over** a config `nodes:` entry per node; the resolved
per-node overrides are threaded via `RunContext.node_overrides` (a
`{node: {instructions, model, agent_dir, duration, idle_timeout_s, options}}`
map) and each agent-node applies its own entry. An unknown node name in `nodes:`
(or a `--instruct typo=…`) is a hard error at build time — it used to be silently
ignored.

**Additive, LAST word.** The run-time instruction is appended AFTER the build-time
per-node instruction (6) and before the work order — the most recent standing
guidance the agent sees. So it overrides earlier instructions by recency, no
special flag needed: *"Ignore the prior instruction about compact tables — produce
the full breakdown instead."*

This is distinct from `start_from` (CLI `--start-from`, a per-invocation entry
point, never persisted in config), though the two pair naturally: when you re-enter
the flow at a node to iterate, you usually also want to tell that node what to do
differently.

## Mapping to the old orchestrator vocabulary

- "stuff currently in the orchestrator" → **(6)** per-node `instructions` and
  **(9)** `inputs`.
- "stuff passed when we start the orchestrator" → the flow's **(3)** standing
  brief plus the **(4)** `-i` addition, and params interpolated into **(9)**.

## Rendering: two seams, one invariant

Channels 2–9 reach a renderer as **parts**, never pre-joined (`PromptParts`).
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

Defaults: `render_prompt` and `render_work_order_xml` (`<KEY>value</KEY>`;
`render_work_order_lines` ships for the pre-0.3 `KEY: value` shape). `render_prompt`
emits a markdown heading per non-empty channel, in the order above:

```
## Run-wide context                     (2)
## Run-wide instructions                (3)
## Additional run-wide instructions     (4)
## Context for this step                (5)
## Instructions for this step           (6)
## Additional instructions for this run (7)
<attempt instruction, verbatim>         (8)
<work order>                            (9)
```

The two "additional" headings are scoped so they never collide: *run-wide* (4)
vs the per-node (7). `PromptParts` also carries `inputs`, the work order still
structured, so an outer renderer may lay the data out itself rather than consume
the inner one's text.

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
invocation directly. The `RunContext.run_instructions` /
`run_additional_instructions` / `run_context` / `node_overrides` /
`one_time_instruction` plumbing lives in `engine.py` (`build_flow`); the CLI
`--instruct` + config `nodes:` handling lives in `cli/commands/run.py`, with the
`nodes` field (a `dict[str, NodeRunConfig]`) on `run_config.py`. A FlowDef's
`run_context` AND `run_instructions` are threaded onto `RunCliContext` by
`cli/app.py` — neither has its declaration honoured any other way under
`run_cli`, so without that wire `run_cli` would silently drop them while
`run_flow` honoured them (the two bugs fixed in 0.4.0).
