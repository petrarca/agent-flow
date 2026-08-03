---
type: Guide
title: Writing agents that work with agent-flow
description: What your opencode agent .md must and can do to be supervised by agent-flow — the control-file contract, from an agent author's view.
tags: [agent-flow, agents, opencode, control-file, writing-guide]
timestamp: 2026-07-23T08:54:40Z
---

# Writing agents for agent-flow

agent-flow runs your agent as a subprocess and reads its outcome from a JSON
control file the agent writes. This is the agent author's view of that contract;
for the internals see [control-file.md](../design/control-file.md).

## What you never write

The library injects a completion-protocol block into every prompt: the absolute
`CONTROL_FILE` path and the JSON shape to write there.

```json
{
  "status": "ok",        // "ok" | "verified" | "error"
  "agent": "your-agent-name",
  "reason": "",          // short explanation, only when status is "error"
  "result": {}           // optional structured data
}
```

Your `.md` should say nothing about this. Do the task and trust that the
protocol is in the prompt.

## What your `.md` must do

1. Do the task — read and write whatever files the job requires.
2. Write the control file as the **final** action, per the injected instructions.
3. Use `status: "ok"` (or `"verified"` for a checking agent) on success, `"error"`
   with a `reason` on failure.

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
Write a concise report to `REPORT` (Write tool) covering …
```

## Structured output

`result` is free-form unless the node declares a
[`result_schema`](../design/result-schema.md), in which case the
injected protocol shows the exact JSON schema to conform to — again, nothing for
your `.md` to restate. Without a schema, put whatever structured facts a gate
might read, or nothing.

## Requesting a re-run

You do **not** document `rerun_required` in your agent's `.md`, and you never
write step names there. The pipeline grants the capability on the node
(`rerun_targets=[...]`), and the library injects a block naming exactly the steps
this agent may ask for — so a renamed step can never leave a stale name behind.

Your `.md` only needs to say **when** to use it, in domain terms:

```markdown
## Requesting a re-run

If the report is unusable (entire sections missing, not merely incomplete),
request a re-run of the step that produced it instead of reporting `verified`.
Say briefly what it must fix. This should be rare.
```

The agent then writes whatever the injected block told it — `true` when only one
step was granted, or `{"target": "...", "instruction": "..."}` when it must
choose. See [Let an agent re-run an earlier step](recipes.md#let-an-agent-re-run-an-earlier-step)
for the pipeline side.

## Always use the paths you are given

Paths in the prompt (`REPORT` and friends) are already absolute — the pipeline
resolved `{run_dir}/report.md` for you. Use them verbatim. opencode's Write tool
resolves a *relative* path against the project root (where `.opencode/` lives),
not the subprocess working directory, so a relative path can land somewhere
unexpected.

## Simulated agents

For demo agents that do no real work (see `examples/.opencode/agent/*.md`), say
so explicitly and forbid tools beyond what the simulation needs: "This is a
SIMULATION; do not read files or access repositories; invent plausible content
based only on the inputs given."
