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

The injected protocol mentions `rerun_required`, but your agent only knows *when*
to use it if you say so. This is how a verifier asks for an earlier step to be
redone:

```markdown
## Requesting a re-run

If the report is unusable (entire sections missing, not merely incomplete),
include `"rerun_required": ["hello"]` in your control JSON instead of a bare
`"verified"` status, so the `hello` step is redone. This should be rare.
```

Use the node's name (`NodeDef(name=...)`). The pipeline side needs a matching
gate: `gate="rerun_on_signal", gate_args={"target": "hello"}` — see
[advanced-recipes.md](advanced-recipes.md#a-verifier-that-can-trigger-a-re-run).

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
