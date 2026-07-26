---
type: Design
title: ServeExecutor — shared opencode serve daemon
description: Design for ServeExecutor, a peer executor that talks to a shared opencode serve HTTP daemon instead of spawning one process per node. Covers motivation, API surface, sidecar contract, directory routing, the scope boundary (daemon management is out of scope), and infrastructure gaps.
tags: [agent-flow, serve-executor, opencode, http, sse, executor]
status: ideation
---

# ServeExecutor — shared `opencode serve` daemon

## Problem

`SubprocessExecutor` spawns a fresh `opencode run` process per node invocation.
When many pipeline processes run concurrently — dozens of flows, each with
multiple nodes — every node pays the full opencode boot cost (~300–500 ms) and
restarts all MCP servers from scratch. At N pipelines × M nodes that is N×M
cold-starts.

`opencode serve` runs as a persistent HTTP+SSE daemon. MCP servers start once
and stay warm. Multiple pipeline processes can point a `ServeExecutor` at the
same shared daemon URL and dispatch all their node invocations as sessions:

```
pipeline A  ──┐
pipeline B  ──┼──► one shared opencode serve ──► MCP servers (warm)
pipeline C  ──┘
```

The gain is purely **resource efficiency**. Orchestration stays entirely in
agent-flow (DAG, gates, retries, typed output). Only execution moves to the
daemon.

---

## Position in the executor hierarchy

`ServeExecutor` is a **peer executor** — same level as `SubprocessExecutor`,
`InProcessExecutor`, and `MockExecutor`:

```
AgentExecutor (ABC)
  ├── SubprocessExecutor   spawn + liveness + kill + sidecar  (via AgentRunner)
  ├── ServeExecutor        shared HTTP daemon + SSE + sidecar  (via ServeClient)
  ├── InProcessExecutor    direct Python call, return value IS verdict
  └── MockExecutor         mock_agent behaviour, return value IS verdict
```

`ServeClient` is `ServeExecutor`'s private wire adapter — analogous to
`AgentRunner` for `SubprocessExecutor`. It owns the HTTP/SSE wire details
(create session, prompt, abort, event stream). It is not a public seam.

`ServeClient` does **not** manage the daemon. The daemon is started and kept
alive by infrastructure outside agent-flow; `ServeClient` only consumes a URL
(see "Scope boundary" below).

The **sidecar family** (`Subprocess` + `Serve`) vs the **return-value family**
(`InProcess` + `Mock`) is the real structural split. Both sidecar executors
poll for a control file and harvest telemetry from an event stream. A shared
base class is a natural future extraction; start flat.

---

## The opencode serve API (relevant surface)

### Session lifecycle

```
POST   /session?directory=<path>          create a session
POST   /session/:id/message?directory=<>  sync prompt (blocks until agent done)
POST   /session/:id/abort?directory=<>    fire-and-forget cancel signal
GET    /global/health                     { "healthy": true, "version": "..." }
```

The `?directory=` query param (or `x-opencode-directory` header) routes every
request to its workspace. opencode resolves config (model, MCP tools,
`.opencode/agent*`) relative to that directory per-request — the same isolation
as `SubprocessExecutor`'s `--dir` flag.

### Sync prompt semantics

`POST /session/:id/message` is **truly blocking**: it holds the HTTP connection
open until the full agent loop (all tool-call turns) finishes, then returns the
final assistant message as a single JSON payload:

```json
{
  "info": {
    "id": "...",
    "sessionID": "...",
    "role": "assistant",
    "cost": 0.0012,
    "tokens": { "input": 400, "output": 120, "reasoning": 0, "cache": {...} },
    "error": null,
    "finish": "stop"
  },
  "parts": [
    { "type": "text", "text": "..." },
    { "type": "tool", "tool": "read", "state": { "status": "completed", ... } }
  ]
}
```

On model error (bad model name, provider failure) the response is **HTTP 400**
with `{ "_tag": "BadRequest" }` — NOT 200 with an error field.

There is **no explicit timeout** on the server side. The executor must enforce
its own deadline (`inv.idle_timeout_s`) and call abort if it fires.

### SSE event stream

```
GET /event?directory=<path>    global SSE stream for this instance
```

All events for all sessions arrive on one stream. Each line:

```
event: message
data: {"id":"<ulid>","type":"session.idle","properties":{"sessionID":"..."}}
```

Key event types for `ServeExecutor`:

| Event | Meaning |
|-------|---------|
| `session.idle` | **The completion signal** — agent loop finished (normal, error, or abort) |
| `session.error` | Runtime error (model not found, provider failure, content filter) |
| `session.next.step.ended` | One LLM step done; carries `cost` + `tokens` for telemetry harvest |
| `message.part.updated` | Tool state transition — maps to `on_event` callback |

**`session.idle` is the only reliable completion signal.** It arrives after
normal completion, after errors, and after abort. After `session.idle` inspect
`info.error` on the prompt response for the actual failure.

**Note:** The v2 API (`/api/session/:id/event?after=<seq>`) provides a
per-session SSE stream with replay. Prefer this for `ServeExecutor` — no
global fan-out, no sessionID filtering needed, and reconnects can replay missed
events from a sequence number.

### Error signal from abort

`POST /session/:id/abort` returns `true` immediately — it does not wait. The
session emits `session.idle` asynchronously after interruption.

---

## Sidecar contract — unchanged

The agent still writes `<node>.control.json` via its Write tool into `run_dir`.
`ServeExecutor` reads it after `session.idle` arrives (same as
`SubprocessExecutor` reads it after process EOF). The status verdict, gate
machinery, `result_schema` validation, and `check_content_status` all work
identically.

The sidecar is an **agent contract**, not a subprocess mechanism.
`ServeExecutor` does not use `message.structured` from the prompt response for
the verdict — that would couple it to opencode's `StructuredOutput` tool
injection and introduce the same re-nudge fragility as other HTTP-based
approaches.

---

## `agent_dir` routing

`inv.agent_dir` maps directly to `?directory=<agent_dir>` on every API call
(create session, prompt, abort). No new field needed — it is already on
`AgentInvocation`.

`agent_dir` is **effectively required** for `ServeExecutor`. Without it, all
sessions share the daemon's own cwd, resolving the same config and potentially
colliding on file writes. The `ServeExecutor` preflight check must flag a
missing `agent_dir` as fatal.

---

## Daemon connection (attach-only)

`ServeClient` **attaches** to an already-running daemon — it never starts one.

```
RunConfig.serve_url = "http://127.0.0.1:4096"
```

`ServeClient` health-checks the URL on first use (`GET /global/health`) and
raises a clear error if unreachable. The daemon is started and kept alive by
whatever is above agent-flow — a shell, a systemd unit, a container, a
service-discovery system. See "Scope boundary".

`ServeClient` is injected into `ServeExecutor` at construction; the executor
itself is stateless.

> **Out of scope (not MVP, possibly never in the library):** auto-starting a
> local daemon for convenience. It is tempting for single-pipeline development
> (spawn `opencode serve --port <auto>`, wait for health, `atexit` teardown),
> but it drags daemon lifecycle into the library. Keep it out until there is a
> concrete need; attach-only is the whole MVP.

---

## Telemetry harvest

`session.next.step.ended` events carry `cost` and `tokens` per LLM step.
`ServeExecutor` subscribes to the SSE stream while the session is active and
accumulates these — same as `SubprocessExecutor` accumulates them from stdout
events via `parse_event`. The `on_event` callback receives neutral `Event`
objects translated from SSE events (same `Event` dataclass, same `kind`/`title`
fields).

---

## Timeout and abort

`ServeExecutor` enforces `inv.idle_timeout_s` as a **wall-clock deadline** from
session creation. If the deadline fires before `session.idle` arrives:

1. Call `POST /session/:id/abort`
2. Wait briefly for `session.idle` on the SSE stream (up to 5 s)
3. Raise `AgentTimeoutError`

This differs from `SubprocessExecutor`'s liveness model (idle = no event for N
seconds). For the serve model a wall-clock cap is simpler and correct — the
daemon owns the agent loop, not agent-flow.

---

## Infrastructure gaps

These are changes needed **beyond the executor itself**:

### `get_executor` hardwired to `SubprocessExecutor`

`runners/__init__.py:get_executor(name)` always wraps in `SubprocessExecutor`.
Under the leaning naming decision (Option B, "To be discussed"), the runtime
name itself selects the executor. `get_executor` gains a second entry that pairs
the runner with a `ServeExecutor` and threads in `serve_url`:

```python
# today
def get_executor(name: str) -> AgentExecutor:
    return SubprocessExecutor(get_runner(name))

# with serve (Option B — compound runtime name)
def get_executor(name: str, *, serve_url: str = "") -> AgentExecutor:
    if name.endswith("-remote"):
        runtime = name.removesuffix("-remote")          # "opencode-remote" -> "opencode"
        return ServeExecutor(get_runner(runtime), url=serve_url)
    return SubprocessExecutor(get_runner(name))
```

`node_builder.py` passes `serve_url` through from `ctx.params`. This keeps
executor selection a single-parameter lookup on the runtime name (the executor
class is decided by the name; `serve_url` is a required companion, not the
selector). The exact call shape depends on the naming sign-off — see
"To be discussed".

### `RunConfig` has no `serve_url` field

One new field, worded neutrally until the naming decision lands:

```python
serve_url: str = Field(
    default="",
    description="URL of a running opencode serve daemon (e.g. 'http://127.0.0.1:4096'). "
                "Required for a remote-execution runtime; the daemon must be started "
                "and kept alive outside agent-flow (attach-only)."
)
```

Wire through CLI as `--serve-url`. No `serve_port` — attach-only, so agent-flow
never binds a port.

### `preflight.py` queries only runners

`preflight.check(runtime, agent_dir)` calls `get_runner(runtime)` →
`runner.preflight_checks()`. For a remote runtime the runner still exists
(`OpenCodeRunner`) but its subprocess checks (`opencode` on PATH, not nested)
are irrelevant. A remote runtime needs different preflight: is the URL
reachable? Is `agent_dir` set and does it have `.opencode/agent*`?

Simplest fix: detect the remote runtime (name suffix / `serve_url` set) in
`preflight.check`; when remote, skip the runner's subprocess checks and instead
check URL reachability + agent layout only.

### No `ServeClient` yet

`ServeClient` is entirely new — the HTTP/SSE wire adapter. Attach-only: it
consumes a URL and never starts or supervises a daemon. No auto-start, no
restart-on-crash.

### `parse_sse_event` on `OpenCodeRunner`

`ServeExecutor` needs to translate raw SSE event dicts to neutral `Event`
objects for the `on_event` callback and telemetry harvest. The translation is
opencode-specific — it belongs in `OpenCodeRunner`, parallel to `parse_event`
(stdout) and `parse_stderr_line` (stderr):

```python
def parse_sse_event(self, event: dict) -> Event:
    """Translate one opencode SSE event dict to a neutral Event."""
```

`ServeExecutor` calls this via `getattr(runner, "parse_sse_event", None)` — the
same optional-method pattern used for `preflight_checks`, `info`, and
`parse_stderr_line`. Runners without it produce no display events (safe default).

---

## Workspace isolation — per run, not per node

Agent-flow nodes within one pipeline run already write to separate subdirs under
`run_dir` (each node's sidecar is `<run_dir>/<node>.control.json`). The
`agent_dir` points at the agent definitions directory — it is read-only from
the agent's perspective (`.opencode/agent/*.md`). File artifacts go to `run_dir`
which is separate.

For **parallel nodes within one run** sharing the same `agent_dir`:
- Agent definition reads: safe (read-only).
- Sidecar writes: safe (keyed by node name under `run_dir`).
- Artifact writes: consumer's responsibility (same as today with subprocess).

For **multiple pipeline runs sharing one daemon**:
- Each pipeline has its own `run_dir` (unique per run by design).
- `agent_dir` may be shared across pipelines of the same type — read-only, safe.
- Agent-flow pipelines read existing code and write reports/sidecars to
  `run_dir`. No git workspace cloning per run is needed: `agent_dir` is a
  static definitions directory; agents do not commit to it.

---

## Scope boundary — agent-flow is a client

Starting, preparing, and managing a daemon is **never** part of the library.
agent-flow integrates as a **consumer** of a companion infrastructure service
that owns all of that.

### The companion infrastructure (out of scope, not built here)

A separate, API-accessible service is responsible for the daemon lifecycle and
the environment it runs in:

- **Provisioning** — starting/pooling `opencode serve` instances, health,
  restart-on-crash, capacity, routing (Consul / Nomad / k8s / a bespoke
  service). agent-flow never sees this.
- **Preparation** — materialising the execution environment before a run: the
  `agent_dir` (agent definitions, skills, commands, opencode config), any
  provider/credential wiring, MCP servers. The service hands back a target that
  is ready to execute against.
- **Artifact plane** — ensuring `run_dir` is on storage reachable by both the
  daemon (agent writes there) and the agent-flow process (reads the sidecar):
  a shared filesystem, a network volume, or an object store with a local cache.

The natural shape is: agent-flow asks the service "give me an execution target
for this run" and receives back the three things it needs — a **URL**, a
prepared **`agent_dir`**, and a writable **`run_dir`**. How the service produces
them is entirely its concern.

### What agent-flow owns

The atomic, self-contained unit: **given a reachable URL, a prepared
`agent_dir`, and a writable `run_dir`, execute one node invocation and return an
`AgentResult`.**

`ServeClient` health-checks the URL on first use and fails fast if unreachable —
that is the extent of agent-flow's involvement with the endpoint. Everything
about how the URL, `agent_dir`, and `run_dir` came to exist is the companion
service's responsibility.

### MVP: a plain HTTP endpoint

For the MVP there is **no companion service**. The three inputs arrive as plain
config on `RunConfig`:

- `serve_url` — a static HTTP URL of an already-running `opencode serve`
  (the consumer starts it however they like)
- `agent_dir` — an existing prepared directory (as today)
- `run_dir` — an existing writable directory (as today)

`ServeExecutor` health-checks `serve_url`, runs against it, done. That is the
entire MVP surface. The companion-service framing above is the eventual shape,
not a prerequisite.

### Integration seam (future, post-MVP)

The companion infrastructure does not *replace* `serve_url` — it *becomes* the
thing `serve_url` points at. As long as it speaks the same HTTP session contract
(create session, prompt, abort, event stream), the endpoint can be:

- MVP: a raw `opencode serve` at a static URL, or
- later: the companion service's endpoint — a front door over a pool of
  prepared daemons, doing routing, provisioning, and workspace preparation
  behind the same API.

`ServeExecutor` cannot tell the difference and never needs to. This is the
whole point of keeping the executor's contract "given a URL, run one
invocation": the URL stays the single input, and the thing behind it grows from
one daemon to managed infrastructure without touching `ServeExecutor`.

An optional provisioning step (ask the service for a target at run start,
release it at run end) would layer *on top* — resolving `serve_url` before the
run — not inside `ServeExecutor`.

## What this is NOT

- Not a change to `AgentRunner` or the subprocess path.
- Not a service discovery or instance management system.
- Not a replacement for `SubprocessExecutor` — both coexist.
- Not needed for a single pipeline with sequential nodes where MCP cold-start
  is negligible vs LLM latency.
- **Not a shift toward a hardcoded workflow engine.** `ServeExecutor` changes
  *how* an agent runs (shared daemon vs subprocess), never *what* the flow is.
  agent-flow stays a **programming model**: the consumer declares an arbitrary
  graph (`Node`s, `depends_on`, parallel groups), writes gates as functions
  (`Continue`/`Restart`/`GoTo`/`Stop`), and passes typed info explicitly
  (`result_schema`, `ctx.result`, `{PARAM}` templates). The engine knows no
  domain vocabulary — no "phases", no "activities", no baked-in pipeline. This
  feature is purely an execution-backend optimisation and leaves the
  declarative programming model untouched.

---

## To be discussed

### Runtime name vs execution mode

Two options for how the runtime string selects `ServeExecutor`.

---

**Option A — two parameters: `runtime` + `serve_url`**

`runtime` names the agent runtime only (`opencode`, `claude-code`, ...).
Presence of `serve_url` switches the executor from `SubprocessExecutor` to
`ServeExecutor`; the runner is the same in both cases.

```
--runtime opencode                                    # subprocess (today)
--runtime opencode --serve-url http://127.0.0.1:4096  # serve daemon
```

`get_executor` can no longer select the executor from the runtime name alone —
`node_builder` participates in construction (already the pattern for
`InProcessExecutor` and `MockExecutor`).

Advantages: `runtime` stays a single axis; `OpenCodeRunner` is fully shared
between both executors; no new runtime name strings to document.

Disadvantages: mode is implicit (presence/absence of `serve_url`); less
discoverable from `--help`.

---

**Option B — compound runtime name: `<runtime>-remote`**

The runtime string encodes both the agent runtime and the execution mode.
`get_executor` maps the string to the executor directly, no second parameter:

```
--runtime opencode           # subprocess per node (today, unchanged)
--runtime opencode-remote    # shared daemon at --serve-url
```

`serve_url` is still a required companion field on `RunConfig` — it tells
`ServeExecutor` where the daemon is. But executor selection is entirely from
the runtime name.

The pattern extends to any future runtime:

```
claude-code           → SubprocessExecutor(ClaudeCodeRunner())
claude-code-remote    → ServeExecutor(ClaudeCodeRunner(), url=serve_url)
```

`ServeExecutor` takes the runner as a constructor arg — it is genuinely
runtime-agnostic. The runner provides `parse_sse_event` the same way it
provides `parse_event` and `parse_stderr_line` today.

Advantages: explicit and discoverable (`opencode-remote` is self-documenting);
`get_executor` remains a single-parameter lookup; the `-remote` suffix is a
consistent convention across all future runtimes.

Disadvantages: introduces a new name string per runtime (`opencode-remote`,
`claude-code-remote`, ...); `-remote` must not collide with actual runner names.

---

**Lean toward Option B** — explicit naming is worth the extra string. The
`-remote` suffix is unambiguous and the pattern scales cleanly to future
runtimes. Needs sign-off before implementation.

---

## Open questions

### 1. SSE threading model — the hardest part

`ServeExecutor.run()` is called from multiple threads simultaneously (one per
parallel node). The global `/event` stream is one HTTP connection per daemon.
Two options:

**Option A — `ServeClient` owns one shared SSE reader thread.**
`ServeClient` starts a single background daemon thread that reads the global
`/event` stream and dispatches each event to a `sessionID → queue` registry.
Each `ServeExecutor.run()` call registers its session ID before sending the
prompt and dequeues from its own `queue.Queue` to detect `session.idle`.

```
ServeClient (shared)
  ├── _sse_thread (daemon) ── reads /event ──► dispatches to per-session queues
  ├── _session_queues: dict[sessionID, Queue]
  └── register(sessionID) / unregister(sessionID)

ServeExecutor.run():
  q = client.register(session_id)
  POST /session/:id/message  (blocks HTTP)  ← in its own thread
  wait on q for session.idle or timeout     ← in the calling thread
  client.unregister(session_id)
```

Problem: the sync prompt blocks the calling thread waiting for HTTP response.
But `session.idle` arrives on SSE before the HTTP response is sent (or
concurrently). Need to handle both: HTTP response for telemetry, SSE queue for
`session.idle` as the authoritative completion signal. The HTTP call and the
SSE wait must run concurrently → needs a second thread per invocation or an
async model.

**Option B — use the v2 per-session SSE stream.**
`GET /api/session/:id/event` is scoped to one session — no fan-out, no shared
reader. Each `ServeExecutor.run()` opens its own SSE connection for its session
and reads until `session.idle`. No shared state in `ServeClient` for the event
path.

```
ServeExecutor.run():
  session_id = POST /api/session               (create)
  prompt_thread = Thread(POST /api/session/:id/prompt)
  sse_thread = Thread(GET /api/session/:id/event → collect until idle)
  both.join(timeout=idle_timeout_s)
  read sidecar
```

Cleaner isolation, no shared state. The cost is one extra HTTP connection per
active node invocation — acceptable since the serve model is only used when
concurrency is high enough to warrant it.

**Recommended: Option B** — simpler, no shared mutable state, naturally
parallel. The v2 API stability question (see below) is the only blocker.

### 2. v2 vs legacy API

The v2 per-session event stream (`/api/session/:id/event?after=<seq>`) is
cleaner for `ServeExecutor`. But the v2 prompt endpoint
(`/api/session/:id/prompt` + `/api/session/:id/wait`) has a different request
shape from the legacy sync prompt. Start with the **legacy sync prompt**
(`POST /session/:id/message`) for the prompt call (well-tested, predictable
blocking behaviour) and the **v2 event stream** for completion detection.
Mixing the two is fine — they share the same session ID space.

### 3. Session cleanup

Sessions accumulate in opencode's SQLite DB (`~/.local/share/opencode/opencode.db`).
Options:
- `DELETE /session/:id` after each `run()` completes (clean, but adds a round-trip)
- Rely on opencode's own `OPENCODE_DISABLE_PRUNE=false` retention policy
- Accept growth — opencode's DB is append-only and the data is small per session

Recommendation: do nothing initially; add explicit cleanup if DB size becomes
a problem in practice.

### 4. Runtime name / execution mode split

See "To be discussed" above.
