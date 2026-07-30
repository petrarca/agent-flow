---
type: Design
title: ServeExecutor — shared opencode serve daemon
description: Design for ServeExecutor, a peer executor that talks to a shared HTTP daemon instead of spawning one process per node. Covers motivation, the opencode serve API surface, an alternative facade-service protocol (own FastAPI layer over any agent, incl. subprocess-only ones like Claude), sidecar/verdict contract, directory routing, the scope boundary, and infrastructure gaps.
tags: [agent-flow, serve-executor, opencode, goose, crush, facade, http, sse, executor]
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

The gain is purely resource efficiency. Orchestration stays entirely in
agent-flow (DAG, gates, retries, typed output). Only execution moves to the
daemon.

---

## Position in the executor hierarchy

`ServeExecutor` is a peer executor — same level as `SubprocessExecutor`,
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

`ServeClient` does not manage the daemon. The daemon is started and kept
alive by infrastructure outside agent-flow; `ServeClient` only consumes a URL
(see "Scope boundary" below).

The sidecar family (`Subprocess` + `Serve`) vs the return-value family
(`InProcess` + `Mock`) is the real structural split. Both sidecar executors
poll for a control file and harvest telemetry from an event stream. A shared
base class is a natural future extraction; start flat.

---

## The pattern is not opencode-specific

The "persistent local HTTP+SSE server sharing a warm process across
invocations" pattern is a small but growing norm among standalone terminal
coding agents. Three ship a near-identical create-session → post-message →
stream-events triad:

| Agent | Server entry | Create session | Prompt | Events |
|-------|-------------|----------------|--------|--------|
| opencode | `opencode serve` | `POST /session` | `POST /session/:id/message` | SSE `/event` |
| Goose | `goosed` / `goose web` | `POST /sessions` | `POST /sessions/:id/messages` | SSE |
| Crush | `internal/server` (unix socket) | `POST /v1/sessions` | agent send | SSE `/v1/.../events` |

A second family exposes the same warm-process benefit over JSON-RPC rather
than HTTP+SSE: Codex CLI (`codex app-server`, a real daemon) and Cursor
(`cursor-agent acp`, ACP over stdio). These would need a different executor
(JSON-RPC/ACP transport), out of scope here.

The rest — Claude Code, Gemini CLI, Amp, Aider — are subprocess-per-invocation
or in-process-SDK only, served by the existing `SubprocessExecutor`.

**Consequence for the design:** `ServeExecutor` is genuinely runtime-agnostic
for the HTTP+SSE family. The runtime-specific bits — endpoint paths
(`/session` vs `/sessions` vs `/v1/sessions`) and event/response shapes — must
be delegated to the runner (via `parse_sse_event` and endpoint-building
methods), NOT hardcoded into `ServeClient`. This mirrors how `AgentRunner`
delegates `build_command`/`parse_event` for the subprocess family. The MVP
targets opencode only, but the seam must not bake opencode's paths into the
generic executor.

---

## Alternative / complement: a facade service (own protocol)

**Status: ideation.** An alternative — or complement — to talking to each
agent's NATIVE server is to put our OWN service in front of the coding agent and
have `ServeExecutor` speak a single protocol WE define. The facade (built on the
existing sonnet-server FastAPI infrastructure) drives the underlying agent
however that agent works, and exposes one uniform surface.

```
agent-flow ──► facade (FastAPI, our protocol) ──► opencode / claude / codex / …
              create/prompt/verdict/abort         subprocess / SDK / native server
```

### Why a facade is attractive

- **Uniform protocol across all agents.** The ecosystem is fragmented:
  opencode/Goose/Crush expose HTTP+SSE, Codex/Cursor expose JSON-RPC,
  Claude/Gemini/Amp expose nothing but subprocess. Native-server support means N
  `RemoteRunner`s with N transports. A facade means ONE `RemoteRunner` speaking
  our protocol; all runtime-specific glue lives in the facade.
- **Claude (and any subprocess-only agent) becomes "remote".** Claude Code has
  no serve mode, so it can never be remote natively. Behind a facade, the facade
  runs `claude -p` (or the Claude Agent SDK) internally and presents it through
  our protocol. The facade normalises "has a server" vs "subprocess only".
- **We own the verdict protocol end to end.** No coping with opencode's
  `info.structured` vs sidecar vs `/file/content`, or each runtime's variant.
  The facade decides how the agent reports and returns a clean, uniform envelope
  — the "verdict transport is runtime-specific" problem collapses into the
  facade.
- **The facade is the natural home for warm-process pooling.** It may keep
  `opencode serve` warm, pool SDK sessions, or spawn subprocesses — its concern.
  agent-flow just sees "a URL that speaks my protocol." This is exactly the
  companion infrastructure already scoped OUT of the library (see "Scope
  boundary") — the facade is that infrastructure, realised.

### How it fits what already exists

Almost nothing changes in agent-flow — the seams already anticipate it:

- `ServeExecutor` stays generic: it speaks HTTP(+SSE) to "a URL".
- The runner for the facade is a SINGLE implementation speaking OUR protocol,
  not opencode's — e.g. a `FacadeRunner` registered under `*-facade` (or simply
  `remote`). Runtime-specific messiness moves OUT of agent-flow into the facade.
- `serve_url` becomes the facade URL. This is the documented forward-compat point
  ("the companion service BECOMES the endpoint `serve_url` points at") made real.
- The verdict is owned by the facade; agent-flow interprets OUR clean response,
  not a runtime's native shape.

### Trade-offs

- **We now own and operate a service** (deploy, health, scale, version) — but
  the companion infrastructure was already accepted as desirable.
- **Per-agent drivers move to the facade.** The runtime-specific complexity does
  not vanish; it relocates from N runners in the library to N drivers in the
  service — the right place (a service concern, evolving independently of
  agent-flow releases).
- **Two-repo effort.** agent-flow gets a small `FacadeRunner` + `ServeExecutor`
  (one protocol). sonnet-server gets the facade service (the real work: per-agent
  drivers, session management, verdict normalisation, optional event relay).
- **Extra hop** agent-flow → facade → agent: negligible vs LLM latency.

### Native-server vs facade — not either/or

The native-opencode-serve path (the rest of this document) is the SIMPLEST
possible facade: a thin/no-op passthrough for local single-daemon use. The
facade is what makes the model GENERAL — uniform protocol, subprocess-only agent
support (Claude), verdict normalisation. Both can coexist: `ServeExecutor`
speaks HTTP+SSE either to a raw `opencode serve` or to our facade, chosen by
which runner + `serve_url` is configured.

### Open protocol question

Should the facade protocol be synchronous (POST prompt → blocks → returns the
verdict, like opencode's sync message — proven simplest) or async + events
(POST prompt → 202 → subscribe/poll for completion — needed only for live
streaming through the facade)? Sync is the likely MVP; async is an
add-on if live `on_event` display through the facade is wanted.

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

`POST /session/:id/message` is truly blocking: it holds the HTTP connection
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

On model error (bad model name, provider failure) the response is HTTP 400
with `{ "_tag": "BadRequest" }` — NOT 200 with an error field.

There is no explicit timeout on the server side. The executor must enforce
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
| `session.idle` | The completion signal — agent loop finished (normal, error, or abort) |
| `session.error` | Runtime error (model not found, provider failure, content filter) |
| `session.next.step.ended` | One LLM step done; carries `cost` + `tokens` for telemetry harvest |
| `message.part.updated` | Tool state transition — maps to `on_event` callback |

> Superseded by live testing — see "Verified against opencode serve 1.18.4".
> The MVP does not consume the SSE stream at all: the blocking sync prompt
> (`POST /session/:id/message`) returns completion + result + telemetry + error
> in one response. The global `/event` stream (with `session.idle`) is only
> relevant for the optional, post-MVP `on_event` live-display path. The v2
> per-session stream is NOT usable with the legacy sync prompt (it carries only
> durable events from v2-initiated prompts).

### Error signal from abort

`POST /session/:id/abort` returns `true` immediately — it does not wait. The
session emits `session.idle` asynchronously after interruption.

---

## The verdict protocol belongs to the runner

The verdict round-trip — HOW the agent is told to report its outcome, and HOW
that outcome is read back — is runtime- and transport-specific. It must NOT be a
library-wide assumption.

Today `build_control_preamble` lives in `protocol/control.py` and
`SubprocessExecutor` hardcodes "the agent writes a sidecar file; read it off
disk". That bakes the sidecar mechanism into the whole library. But:

- the sidecar preamble ("write CONTROL_FILE with the Write tool") is only
  universal for runtimes with a Write tool + a filesystem;
- reading the verdict back over HTTP (`GET /file/content`) is opencode-specific;
- an inline structured verdict (`info.structured` via `format:{json_schema}`) is
  opencode-specific too.

So there is NO single verdict mechanism that fits every runtime and transport.
The resolution: the runner owns both ends of its verdict protocol.

```
RunnerBase
  ├── spec()
  ├── parse_event(raw) -> Event
  └── verdict protocol (runtime + transport owned):
        build_verdict_preamble(agent, node, run_dir, result_schema) -> str
        harvest_verdict(ctx) -> control_dict        # ctx: response / paths / client
```

- `build_verdict_preamble` — the instruction block telling the agent how to
  report (prepended to the prompt, exactly where `build_control_preamble` output
  goes today).
- `harvest_verdict` — reads the outcome back the runner's own way and returns the
  neutral control envelope (`status`/`agent`/`reason`/`rerun_required`/`result`).

The executors become verdict-agnostic — they orchestrate, the runner decides:

```
SubprocessExecutor.run(inv):
    preamble = runner.build_verdict_preamble(...)
    <spawn + supervise>
    control  = runner.harvest_verdict(<sidecar path / proc>)
    assemble_result(control) + check_content_status      # shared, unchanged

ServeExecutor.run(inv):
    preamble = runner.build_verdict_preamble(...)
    resp = POST /session/:id/message (blocking)
    control  = runner.harvest_verdict(<resp / client>)
    assemble_result(control) + check_content_status      # shared, unchanged
```

### What each runner does

- **`OpenCodeRunner` (subprocess)** — `build_verdict_preamble` returns the
  sidecar instruction (the current `build_control_preamble` output, verbatim);
  `harvest_verdict` reads `<run_dir>/<node>.control.json` off disk. **Behaviour
  identical to today**, just relocated from `core` + `SubprocessExecutor` into
  the runner.
- **`OpenCodeRemoteRunner`** — owns the remote choice. MVP: same sidecar
  preamble, harvest by reading the file back via opencode's `GET /file/content`
  (the daemon reads its own disk — no shared mount needed). Later, optionally,
  the C.2 structured-output path (`format:{json_schema}` → `info.structured`) if
  it proves worthwhile — a change entirely inside this runner.
- **`GooseRemoteRunner` / others** — their own preamble + harvest, whatever their
  runtime supports.

`build_control_preamble` in `protocol/control.py` does NOT disappear — it
becomes a shared HELPER the sidecar-style runners choose to call, not a
library-wide contract. `result_schema` still flows through it (embedded in the
preamble) the same way.

### Design it in the existing structure now

This refactor is behaviour-preserving for the subprocess path and can land
BEFORE `ServeExecutor` exists:

1. Add `build_verdict_preamble` + `harvest_verdict` to the runner protocol
   (`RunnerBase`) as optional methods (getattr/hasattr, like `preflight_checks`).
2. Implement them on `OpenCodeRunner` by moving the sidecar preamble + sidecar
   read out of `SubprocessExecutor` / `agent_runtime` into the runner.
3. `SubprocessExecutor` calls `runner.build_verdict_preamble(...)` /
   `runner.harvest_verdict(...)` instead of hardcoding the sidecar — with a
   fallback to the current behaviour so nothing breaks during migration.
4. Tests stay green (same sidecar behaviour, new call path).

Then `ServeExecutor` + `OpenCodeRemoteRunner` slot in with the verdict protocol
already a runner concern.

---

## `agent_dir` routing

`inv.agent_dir` maps directly to `?directory=<agent_dir>` on every API call
(create session, prompt, abort). No new field needed — it is already on
`AgentInvocation`.

`agent_dir` is effectively required for `ServeExecutor`. Without it, all
sessions share the daemon's own cwd, resolving the same config and potentially
colliding on file writes. The `ServeExecutor` preflight check must flag a
missing `agent_dir` as fatal.

---

## Daemon connection (attach-only)

`ServeClient` attaches to an already-running daemon — it never starts one.

```
RunConfig.serve_url = "http://127.0.0.1:4096"
```

`ServeClient` health-checks the URL on first use (`GET /global/health`) and
raises a clear error if unreachable. The daemon is started and kept alive by
whatever is above agent-flow — a shell, a systemd unit, a container, a
service-discovery system. See "Scope boundary".

`ServeClient` is injected into `ServeExecutor` at construction; the executor
itself is stateless.

> Out of scope (not MVP, possibly never in the library): auto-starting a
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

`ServeExecutor` enforces `inv.idle_timeout_s` as a client-side HTTP timeout
on the blocking `POST /session/:id/message` call. If it fires:

1. Call `POST /session/:id/abort`
2. Raise `AgentTimeoutError`

This differs from `SubprocessExecutor`'s liveness model (idle = no event for N
seconds). For the serve model a wall-clock cap on the blocking call is simpler
and correct — the daemon owns the agent loop, not agent-flow. (Verified: the
sync prompt blocks until the loop completes, so the HTTP timeout is the natural
deadline.)

---

## Infrastructure gaps

These are changes needed beyond the executor itself:

### `get_executor` transport dispatch — LANDED

This gap is closed. `runners/__init__.py:get_executor(name)` no longer hardwires
`SubprocessExecutor`: it branches on `spec.transport`, enforces `needs_endpoint`,
and lazily imports `ServeExecutor` for the `http-sse` path — a module that does
not exist yet, so reaching that branch raises `ModuleNotFoundError` (pinned by
`tests/unit/test_options.py`). The remaining work is the executor itself, not
the factory.

`SubprocessExecutor` has also moved to `runners/subprocess_exec.py`, beside the
siblings a `serve_executor.py` would join, so the factory is a flat dispatch
over one package rather than an import back into `core/`.

The original sketch of the change, kept for the shape of the `serve_url`
threading:

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

`node_builder/` passes `serve_url` through from `ctx.params`. This keeps
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

For parallel nodes within one run sharing the same `agent_dir`:
- Agent definition reads: safe (read-only).
- Sidecar writes: safe (keyed by node name under `run_dir`).
- Artifact writes: consumer's responsibility (same as today with subprocess).

For multiple pipeline runs sharing one daemon:
- Each pipeline has its own `run_dir` (unique per run by design).
- `agent_dir` may be shared across pipelines of the same type — read-only, safe.
- Agent-flow pipelines read existing code and write reports/sidecars to
  `run_dir`. No git workspace cloning per run is needed: `agent_dir` is a
  static definitions directory; agents do not commit to it.

---

## Scope boundary — agent-flow is a client

Starting, preparing, and managing a daemon is never part of the library.
agent-flow integrates as a consumer of a companion infrastructure service
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
for this run" and receives back the three things it needs — a URL, a
prepared `agent_dir`, and a writable `run_dir`. How the service produces
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

For the MVP there is no companion service. The three inputs arrive as plain
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
  agent-flow stays a programming model: the consumer declares an arbitrary
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
genuine cross-runtime convention for the HTTP+SSE server family — it applies to
`goose-remote` and `crush-remote` as well (see "The pattern is not
opencode-specific"), each mapping the *same* `ServeExecutor` to a different
runner. It correctly does NOT apply to subprocess-only runtimes (Claude Code,
Gemini, Amp), which have no daemon.

Disadvantages: introduces a new name string per HTTP-server runtime; `-remote`
must not collide with actual runner names.

---

**Lean toward Option B.** The ecosystem finding confirms `-remote` is a
meaningful convention, not just opencode sugar: opencode, Goose, and Crush all
have HTTP+SSE server modes that `ServeExecutor` can serve via per-runtime
runner adapters. The suffix marks "this runtime, in its remote-daemon mode."
Needs sign-off before implementation.

---

## Verified against opencode serve 1.18.4

The opencode serve API was probed live to settle the open questions. Full
findings — health, the blocking sync prompt (completion + result + telemetry in
one response), directory routing, both SSE streams, the unimplemented `wait`,
the three verdict-retrieval options (sidecar / file API / structured output),
and agent-discovery caching — are in
[`../research/opencode-serve-api.md`](../research/opencode-serve-api.md).

Decisions that flow from those findings:

- **Completion**: the blocking `POST /session/:id/message` returns completion +
  result + telemetry + error in one payload — no SSE threading needed for the
  basic run-to-completion flow.
- **API surface**: use the legacy API (`/session/:id/message` blocking; the
  global `/event` stream only if live `on_event` display is later added). The v2
  per-session stream is empty for legacy prompts and `wait` is unimplemented in
  1.18.4.
- **Verdict**: only the sidecar-on-shared-filesystem path is runtime-agnostic;
  `/file/content` read-back and inline `info.structured` are opencode-specific
  enhancements — which is exactly why the verdict protocol belongs to the runner
  (see above).
- **Timeout**: client-side HTTP timeout on the blocking call; on fire,
  `POST /session/:id/abort`.

---

## Open questions

### Runtime name / execution mode split

See "To be discussed" above (Option B — `opencode-remote` — leaning).
