---
type: Research
title: opencode serve API — empirical findings (v1.18.4)
description: Live-probed behaviour of the opencode serve HTTP+SSE API — health, session lifecycle, the blocking sync prompt, directory routing, both SSE streams, the unimplemented wait, verdict-retrieval options (sidecar / file API / structured output), and agent-discovery caching. Reference evidence for the ServeExecutor design.
tags: [agent-flow, research, opencode, serve, http, sse, api]
status: findings
---

# opencode serve API — empirical findings (v1.18.4)

Findings from probing a running `opencode serve` (v1.18.4) directly — health,
session create, the blocking sync prompt, both SSE streams, `wait`, abort,
delete, directory routing, verdict-retrieval options, and agent-discovery
caching. This is the evidence behind the decisions in
[`../design/serve-executor.md`](../design/serve-executor.md);
that document is the design, this one is the reference.

Probed against a server started with `opencode serve` (unauthenticated, warning
printed) listening on a random port.

---

## Health

`GET /global/health` → `{"healthy":true,"version":"1.18.4"}`. Immediate,
unauthenticated. (`GET /api/health` → `{"healthy":true}`, no version.)
Use `/global/health` for a client's first-use reachability check.

There is also an OpenAPI spec at `GET /doc` (~479 KB) enumerating every route.

---

## The sync prompt IS the completion mechanism

`POST /session/:id/message` with
`{"agent":"...","parts":[{"type":"text","text":"..."}]}` **blocks until the
agent loop fully completes** (~3–5 s in tests) and returns the whole result in
one JSON payload:

```json
{
  "info": {
    "role": "assistant",
    "finish": "stop",
    "error": null,
    "cost": 0,
    "tokens": {"total": 15087, "input": 3, "output": 5, "reasoning": 0,
               "cache": {"write": 15079, "read": 0}},
    "modelID": "...", "providerID": "...", "sessionID": "...", "id": "..."
  },
  "parts": [ {"type": "step-start"}, {"type": "text", "text": "pong"},
             {"type": "step-finish"} ]
}
```

This single blocking response gives completion + result + telemetry + error
at once. No SSE threading is needed to detect `session.idle` for the basic
run-to-completion flow:

```
create   = POST /session?directory=<agent_dir>
resp     = POST /session/:id/message?directory=<agent_dir>   (BLOCKS until done)
           # resp.info.error → runtime failure; resp.info.tokens/cost → telemetry
cleanup  = DELETE /session/:id
```

Timeout: enforce a client-side HTTP timeout on the blocking call; on fire,
`POST /session/:id/abort`.

On a model error (e.g. bad model name) the response is HTTP 400 with
`{"_tag":"BadRequest"}` — NOT 200 with an error field. (A transient config
issue in the probe host — `max_tokens is 0` — surfaced as HTTP 500
`UnknownError` with a `ref`; unrelated to the protocol.)

---

## SSE streams

- **Legacy global `GET /event`** — works; emits `session.idle` (the completion
  signal), plus `message.updated`, `message.part.updated`, `session.status`,
  `session.diff`, `session.updated`. GLOBAL: one stream for all sessions; filter
  by `properties.sessionID`. Envelope: `event: message` + `data:` JSON
  `{id,type,properties}`. This is the stream to use for live `on_event` display.

- **v2 per-session `GET /api/session/:id/event?after=<seq>`** — carries only
  **durable** events, and only when the prompt was sent via the v2 endpoint
  (`POST /api/session/:id/prompt`). Emits `session.next.*`
  (`prompt.admitted`, `prompted`, `step.started`, `step.ended`/`step.failed`)
  but NOT `session.idle`. A legacy-prompt session produces NOTHING on it
  (returned 0 bytes in tests). Not usable alongside the legacy sync prompt.

- **`POST /api/session/:id/wait`** ("block until idle") returns **503
  `ServiceUnavailableError` "Session wait is not available yet"** in 1.18.4 —
  declared in the OpenAPI spec but unimplemented. Do not rely on it.

**Decision:** use the legacy API throughout — `POST /session/:id/message`
(blocking) for run + result + telemetry, and (only if live display is added) the
legacy global `/event` stream for `on_event`. The v2 surface is inconsistent in
this build.

---

## Directory routing — verified

The `?directory=<path>` query param (or `x-opencode-directory` header) routes
every request to that working directory. The daemon resolves agent definitions
from `<directory>/.opencode/agent/*.md` per request, on its own filesystem,
with no restart:

- A `hello` agent created in `/tmp/test-agents/.opencode/agent/hello.md` appeared
  in `GET /agent?directory=/tmp/test-agents` but NOT in the bare `GET /agent`
  (which lists only the built-ins: `build`, `general`, `plan`, `explore`, …).
- A full run — `POST /session?directory=…` (agent `hello`) →
  `POST /session/:id/message?directory=…` → text reply — succeeded end-to-end
  against a daemon started in a different directory (`$HOME`).
- `?directory=` must be on EVERY call: create, message, abort, delete.

So `agent_dir` maps directly to `?directory=` — the HTTP analogue of the
subprocess `--dir` flag.

### Agent discovery is CACHED per directory

Adding a new `.md` to a directory the daemon has already scanned does NOT
refresh its agent list — the new agent is "not found", and prompting a
not-found agent returns HTTP 500. A fresh directory picks up all its
agents on first scan. Implication: whatever materialises agents into a directory
must do so BEFORE that directory is first accessed (or use a fresh directory).

---

## Verdict retrieval — three options, all verified

How agent-flow gets the control verdict back. Only the first is runtime-agnostic.

### A — sidecar on a shared filesystem (runtime-agnostic)

The agent writes `<run_dir>/<node>.control.json` with its Write tool; the reader
opens it off disk. Requires `run_dir` reachable by both the daemon (writer) and
the reader. Universal — any runtime with a Write tool + filesystem. The preamble
is the existing sidecar preamble; behaviour identical to the subprocess path.

### B — read the sidecar back over the file API (opencode-specific)

`GET /file/content?directory=<dir>&path=<node>.control.json` →
`{"type":"text","content":"<sidecar JSON as a string>"}`. Verified: the
daemon reads its OWN filesystem and returns the bytes over HTTP, so no shared
mount is needed for the control plane. Endpoint is opencode-specific.

### C — inline structured verdict (opencode-specific)

`POST /session/:id/message` with
`format: {type:"json_schema", schema:{…}}` makes opencode inject a
`StructuredOutput` tool, force the model to call it, and validate the result
server-side. The validated envelope lands in `info.structured` on the
blocking response — a dedicated field, no text scraping, no file at all. Two
variants tested:

- **C.1 (plain text, no schema)** — the agent emits the JSON as its final text
  part; parses with `json.loads`, but the model DRIFTED the schema
  (`"status":"success"` instead of `"ok"`, `result` a string not an object).
  Fragile.
- **C.2 (`format:{json_schema}`)** — `info.structured` carried the exact,
  schema-conforming envelope (`status:"ok"`, `result:{answer:"Paris"}`);
  `finish` was `"tool-calls"` (the tool path). Reliable, server-enforced,
  filesystem-free.

**Note on transport-specificity:** both B (`/file/content`) and C
(`info.structured` / `format`) are opencode API features — NOT universal. The
only runtime-agnostic verdict mechanism is A (sidecar on a filesystem both sides
reach). B and C are opencode-specific enhancements that relax the shared-FS
requirement. This is why the verdict protocol belongs to the runner (see the
design doc): each runner owns how its agent reports and how the verdict is
harvested.

### Session cleanup — verified

`DELETE /session/:id` returns 200 and works. Call it after each run; keeps
opencode's SQLite DB (`~/.local/share/opencode/opencode.db`) from accumulating
sessions.

---

## Endpoint quick-reference (legacy)

| Endpoint | Purpose |
|----------|---------|
| `GET /global/health` | reachability + version |
| `GET /agent?directory=<dir>` | list resolvable agents for a directory |
| `POST /session?directory=<dir>` | create session → `{id}` |
| `POST /session/:id/message?directory=<dir>` | blocking prompt → `{info,parts}` |
| `POST /session/:id/prompt_async?directory=<dir>` | fire-and-forget → 204 |
| `POST /session/:id/abort?directory=<dir>` | cancel (returns immediately) |
| `DELETE /session/:id?directory=<dir>` | delete session (cleanup) |
| `GET /event?directory=<dir>` | global SSE stream (filter by sessionID) |
| `GET /file/content?directory=<dir>&path=<p>` | read a file off the daemon's FS |
