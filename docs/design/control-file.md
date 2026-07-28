---
type: Concept
title: The control file — the agent-to-orchestrator contract
description: The status sidecar every agent writes; its minimal envelope plus an opaque result payload; and library-injected protocol.
tags: [agent-flow, control-file, sidecar, contract, protocol-injection]
timestamp: 2026-07-23T07:51:35Z
---

# The control file (status sidecar)

The control file is the one thing the library reads back from an agent. As
its final action, a subprocess agent writes a JSON file to `CONTROL_FILE`;
`SubprocessExecutor` reads it to determine the verdict. It is the universal
status contract — identical across every runtime — so success does not depend on
parsing any vendor-specific event schema. The same envelope is produced under the
`--mock-agents` mode, where `MockExecutor` writes it to the same path directly
(see "Two writers, one contract" below).

## Shape: minimal envelope + opaque `result`

```json
{
  "status": "ok",
  "agent": "tech-stack-analyst",
  "reason": "",
  "rerun_required": ["tech-stack-analyst"],
  "result": { "summary": "…", "languages": ["Python", "TypeScript"] }
}
```

**Envelope — the engine reads these:**

| Field | Meaning |
|---|---|
| `status` | the verdict: `ok` / `verified` / `error` (required). |
| `agent` | the agent's own name. |
| `reason` | short explanation, for `error`. |
| `rerun_required` | optional list of agents — a flow-control signal a [gate](gates.md) may consume (e.g. a verifier asking an analyst to re-run). |

**Payload — only gates/consumers read this:**

| Field | Meaning |
|---|---|
| `result` | free-form object with agent-specific structured data (e.g. `issues_found`, `key_points`). The engine never looks inside. |

## No `artifact` field — deliberately

There is intentionally no `artifact`/`artifacts` field. What an agent
produces is expressed in the files it was told to write (via `REPORT` etc. in
its [work order](input-plane.md)), not reported back through the control file. A
consumer that wants to check "did the file land?" stats the path it already knows
— in a [gate](gates.md) (`require_file`) — rather than learning it back from the
agent. This keeps the library free of any knowledge of what an agent produces.

## The verdict rule

- Sidecar present, `status` in {`ok`, `verified`} → success.
- Sidecar present, other `status` → `AgentContentFailedError`.
- Sidecar absent → error (`no control sidecar written`). The engine does not
  fall back to inspecting artifacts.

`rerun_required` is the only envelope field beyond the verdict that the library
surfaces, and even then only a gate acts on it. Everything domain-specific lives
in `result` and is opaque.

## Protocol injection — the contract lives in ONE place

Agents do not restate the control-file shape in their `.md`. The library
builds the completion-protocol block (`build_control_preamble`) and
`SubprocessExecutor` injects it into the prompt automatically whenever a
`control_file` is set. It carries:

- a `CONTROL_FILE: <path>` line (so the subprocess agent locates the sidecar), and
- the exact envelope shape to write, plus (optionally) the
  [result schema](result-schema.md) to conform to.

So an agent `.md` carries only its DOMAIN instructions; the "how to signal
completion" part is inherited. Change the contract in one function and every
agent follows. See the composition order in [input-plane.md](input-plane.md).

**`rerun_required` is mentioned in the injected preamble** (as an optional
field, with guidance that it should be rare and only used when the agent's own
instructions say when/why), because a real agent has no other way to discover
that field exists — it cannot see [`gates.rerun_on_signal`](gates.md). Mentioning
it in the shared preamble is necessary but not sufficient: an agent still only
knows to set it if its own `.md` says so for its specific task (naming which
step to re-run). See `examples/.opencode/agent/*-verifier.md`.

## Two writers, one contract

The sidecar has two producers, and the envelope shape and verdict rule are
identical for both:

- A subprocess agent (opencode, …) writes it via its Write tool, told where
  by the injected `CONTROL_FILE` preamble; `SubprocessExecutor` reads it back.
- A `mock_agent` (under `--mock-agents`) simply *returns* the envelope, and
  `MockExecutor` persists it to the same default path (`<node|agent>.control.json`
  under `run_dir`) — no preamble, no prompt-parsing. See
  [mock-agent.md](mock-agent.md).

Both funnel the status through the same shared policy
(`AgentExecutor.check_content_status`), so a bad status behaves identically
regardless of which executor produced the envelope. (Distinct from the test-only
subprocess stub `core/_mock_agent.py`, which *does* read `CONTROL_FILE` from the
prompt — it exists only to exercise subprocess supervision.)

## Where it lives

`src/agent_flow/core/control_protocol.py` (`build_control_preamble`, also
re-exported as `agent_flow.build_control_preamble`),
`src/agent_flow/core/report_signals.py` (`rerun_targets`, `produced`), and
the verdict/status policy shared across executors in
`src/agent_flow/runners/executor.py` (`AgentExecutor.assemble_result` /
`check_content_status`), invoked by `SubprocessExecutor`
(`src/agent_flow/core/agent_runtime.py`) and `MockExecutor`
(`src/agent_flow/runners/mock_exec.py`).
