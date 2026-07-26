---
type: Concept
title: Agent supervision — liveness, not wall-clock
description: How run_agent spawns and supervises one agent subprocess by liveness, the AgentRunner seam, and why --format json.
tags: [agent-flow, supervision, run_agent, liveness, runners, opencode]
timestamp: 2026-07-23T07:51:35Z
---

# Agent supervision (Tier 1)

`run_agent` is the engine core: it spawns ONE agent as an OS subprocess via an
`AgentRunner`, supervises it by **liveness** (not a wall-clock timeout), kills it
on stale, and reads its outcome from the [control sidecar](control-file.md). It
is Prefect-agnostic and reused verbatim across all tiers.

Two directories, kept separate: **`run_dir`** is where the control sidecar is
written and the base for relative artifact paths; **`agent_dir`** (optional) is
where the runtime finds agent DEFINITIONS — for opencode it becomes `--dir`, and
`run_agent` sets the subprocess cwd to it. When `agent_dir` is unset the
subprocess inherits the current cwd. Nothing ever chdir's into `run_dir`; it is
not a working directory in the OS sense.

## Liveness, not wall-clock

- The runtime (opencode) is invoked with `--format json`, emitting an NDJSON
  **event stream**. Each line is a **heartbeat**.
- An **idle timer** (`idle_timeout_s`) is reset on every event. The agent is
  killed **only** when it has emitted no event *and* written no sidecar for the
  whole idle window. There is **no absolute wall-clock cap** — an agent that
  keeps making progress runs as long as it needs.
- **Completion** is detected the moment the sidecar appears on disk (or a
  terminal `step-finish` with `reason == "stop"`), so a **done-but-lingering**
  process is finished immediately rather than waited on (opencode does not
  always exit promptly after the work is done).
- On any stop the child **process group** is killed (SIGTERM → SIGKILL →
  `proc.kill()`), so helper children never linger.

This is agent-runtime supervision that no general-purpose orchestrator provides;
it is the heart of Tier 1.

## The verdict is the sidecar — nothing else

`run_agent` keys success solely on the control sidecar's `status`. If the sidecar
is absent, the run is an **error**. The engine never inspects an agent's
artifacts to guess success — that is a flow decision for a [gate](gates.md), one
layer up. Telemetry (tokens/cost) is harvested from the event stream into the
returned `AgentResult`.

Failure classification:

- `AgentTimeoutError` — went stale (idle) with no valid sidecar. Transient;
  the outer backend retry may re-run it.
- `AgentContentFailedError` — sidecar reports a non-ok status. A content
  failure; not retried by the runtime's classification.
- `AgentCrashError` — process-level crash. Transient; retryable.

## Two complementary layers of timeout/retry

- **Inside the task (ours, runtime-aware):** idle-timer liveness, process-group
  kill, sidecar classification. Decides "is this run healthy / done / hung."
- **Around the task (the backend's, runtime-blind):** retry the whole task,
  a coarse task-timeout backstop, concurrency limits. Decides "run it again /
  don't run too many / give up."

Switching backend replaces only the outer layer; the inner supervision is
untouched.

## The `AgentRunner` seam (pluggable runtime)

A runtime differs from the shared core in exactly two things, captured by a
tiny protocol:

```python
class AgentRunner(Protocol):
    name: str
    def build_command(self, inv: AgentInvocation) -> list[str]: ...  # how to launch one agent
    def parse_event(self, line: str) -> Event: ...                   # liveness/telemetry from stdout
```

Everything else — supervision, kill, sidecar reading, telemetry, the DAG — is
runtime-agnostic and written once. `AgentInvocation` carries the agent **name**
and its resolved **instructions** separately, so a runner materialises identity
its own way: opencode via `--agent`; a runner without named agents (Claude Code)
via `--append-system-prompt`. A runtime with no structured stream returns
`Event.none()` and relies purely on sidecar + idle-timer + exit code — the
domain-free test stub `core/_mock_agent.py` (driven as a subprocess only in
supervision tests) exercises exactly that stale/kill path.

**The honest limit — abstract the runtime, NOT the agents.** The mechanism
(spawn/supervise/parse/sidecar) is fully abstractable. The *content* — agent
names, `.md` bodies, persona — is genuinely runtime-specific. So the runtime is
pluggable via `AgentRunner`; the agent set is a property of a (pipeline × runtime)
pairing, not abstracted away.

## Why `--format json` (verified empirically)

opencode offers two formats — `default` (human-formatted) and `json` — plus a
`--print-logs` flag. We use `--format json`, and this was checked by capturing
real output, not assumed:

| | `--format json` | `default` (piped) | `default --print-logs` |
|---|---|---|---|
| Live heartbeat during the run | yes, per event | **none** — silent until the end | yes (logfmt on stderr) |
| stdout content | every step/tool/text event | only the **final message** | only the final message |
| Machine-parseable | yes (typed `part` union) | n/a | logfmt **+ ANSI** |
| Tokens / cost | yes | no | no |

Decisive fact: **piped `default` output is not a progress stream.** On a non-TTY
(which the orchestrator always is), opencode emits only the assistant's final
message, once, at the end. Under `default` the process is silent for the whole
run — which the liveness supervisor would read as "no heartbeat" and kill.
`--format json` is what makes a headless agent observable at all, and it is the
only single clean channel carrying both the heartbeat and telemetry.
`--print-logs` is a real middle ground but strictly worse: logfmt + ANSI on
stderr, no tokens/cost, needing stderr/stdout split. It stays a debug aid, not
the supervision source.

Because JSON is verbose, we do NOT dump it raw. `parse_event` extracts the
supervision fields plus a runner-agnostic **neutral display view** (kind/title/
detail/status) and keeps the raw line; the [CLI](cli-events.md) renders that
neutral view to one readable line when `--show-events` is on.

## Where it lives

`src/agent_flow/core/agent_runtime.py` holds `SubprocessExecutor` (spawn +
`_supervise` + kill + sidecar read) and the `run_agent` shim that delegates to
it. The `src/agent_flow/runners/` package holds the seam types: `executor.py`
(`AgentExecutor` ABC, `AgentResult`, the shared `assemble_result` /
`check_content_status`, and the `AgentTimeoutError` / `AgentContentFailedError` /
`AgentCrashError` classes), `base.py` (`AgentRunner`, `AgentInvocation`, `Event`,
`compose_prompt`), `opencode.py` (`OpenCodeRunner`), and `__init__.py`
(`get_runner` / `get_executor`). Mock is not a runner: `MockExecutor`
(`runners/mock_exec.py`) is a sibling executor selected by the `--mock-agents`
mode, and `core/_mock_agent.py` is only a domain-free subprocess stub for
supervision tests.
