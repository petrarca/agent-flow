---
type: Guide
title: Writing agents that work with agent-flow
description: What your opencode agent .md must and can do to be supervised by agent-flow — the control-file contract, from an agent author's view.
tags: [agent-flow, agents, opencode, control-file, writing-guide]
timestamp: 2026-07-23T08:54:40Z
---

# Writing agents for agent-flow

`agent-flow` supervises your agent as a subprocess and reads its outcome from a
JSON **control file** it writes. This is the agent-author's view of that
contract. (For the library-internals view, see
[control-file.md](../design/orchestrator/control-file.md).)

## What you never have to write

The library **injects** a completion-protocol block into every prompt
automatically. Your agent `.md` never states the control-file shape — it looks
like this when injected (you'll see it prepended to your prompt at run time):

```
## Completion protocol (required)

CONTROL_FILE: /abs/path/to/your-node.control.json

When you finish, write a JSON control file to the CONTROL_FILE path above.
Use the Write tool (write the file — do NOT print the JSON to stdout).
Write it as your FINAL action, exactly one JSON object:

  {
    "status": "ok",            // "ok" | "verified" | "error"
    "agent": "your-agent-name",
    "reason": "",              // short explanation, only if status is "error"
    "result": {}               // optional: agent-specific structured data
  }

If you cannot complete the task, write status "error" with a "reason".

Optional field "rerun_required": a JSON array of step names that must be
re-run before this work is trusted (...)
```

Your `.md` should say nothing about this — just do the actual task and trust
that the protocol is already in the prompt.

## What your `.md` must do

1. **Do the task** — read/write whatever files your job requires (analyze
   something, verify something, extract something).
2. **Write the control file as your FINAL action**, per the injected
   instructions above (you don't restate the shape, just do it).
3. Use `status: "ok"` (or `"verified"` for a checking-style agent) on success,
   `"error"` with a `"reason"` on failure.

### A minimal "writer" agent

```markdown
---
description: Analyzes X and writes a report.
mode: primary
permission: { edit: allow, bash: deny, webfetch: deny }
---

# X analyst

## Input (from the prompt)
- `REPORT` — absolute path to write the markdown report.

## Task
Write a concise report to `REPORT` (Write tool) covering ... (your domain
instructions here).
```

## Using `result` (optional structured output)

`result` is free-form **unless** the node declares a
[`result_schema`](../design/orchestrator/result-schema.md) — in which case the
injected protocol also shows you the exact JSON schema to conform to, and your
`.md` doesn't need to mention it either. If no schema is declared, put whatever
structured facts are useful for a gate to read (counts, a short summary,
whatever) — or nothing.

## Using `rerun_required` (optional — most agents never need this)

The injected protocol *mentions* `rerun_required` exists, but that alone is not
enough — your agent only knows **when** to use it if its own instructions say
so, naming the specific step. This is how a "verifier"-style agent asks the
orchestrator to redo an earlier step:

```markdown
## Requesting a re-run

If the report is so incomplete that a placeholder is not enough (e.g. entire
sections are missing), include `"rerun_required": ["hello"]` in your control
JSON instead of a bare `"verified"` status, so the `hello` step is redone. This
should be rare — only for a genuinely unusable report.
```

Replace `"hello"` with the name of the node you want redone (the pipeline's
`agent_node(name=...)` — see [getting-started.md](getting-started.md)). The
corresponding node also needs a `rerun_on_signal(target="hello")` gate — see
[recipes.md](recipes.md#a-verifier-that-can-trigger-a-re-run).

## Gotcha: use absolute paths for files

The prompt gives you paths like `REPORT` already resolved to absolute paths by
the pipeline declaration (`{run_dir}/report.md` templates to an absolute path).
**Always use the exact path given, don't make it relative** — opencode's Write
tool resolves a relative path against the *project root* (where `.opencode/`
lives), not the subprocess's working directory, so a relative path can silently
land somewhere unexpected.

## Simulated / demo agents

If you're building a demo where the agent doesn't do real work (see
`examples/.opencode/agent/*.md`), say so explicitly and forbid
tool use beyond what the simulation needs — e.g. "This is a SIMULATION; do not
read files or access repositories; invent plausible content based only on the
inputs given."
