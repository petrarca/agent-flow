---
type: Usage Overview
title: Using agent-flow — for consumers of the library
description: Task-oriented documentation for building your own pipeline on agent-flow. Start here.
tags: [agent-flow, usage, guide, getting-started, consumer-documentation]
timestamp: 2026-07-23T08:54:40Z
---

# Using agent-flow

How to build a pipeline on agent-flow. For why it is built this way, see the
[design docs](../design/index.md).

## Guides

| Guide | Read this when… |
|---|---|
| [getting-started.md](getting-started.md) | Starting from zero: install, write a pipeline, run it. |
| [writing-agents.md](writing-agents.md) | Writing the opencode agent `.md` files agent-flow supervises. |
| [recipes.md](recipes.md) | You need something specific: a re-run loop, parallel steps, typed output, custom logic. |
| [advanced-recipes.md](advanced-recipes.md) | Parallel steps, exports, partial runs, prompt rendering, backends, or dropping below the declarative surface. |

## What you write

1. **Agent `.md` files** — the actual work. See [writing-agents.md](writing-agents.md).
2. **A pipeline declaration** — a `FlowDef` of `NodeDef`s, wired with `depends_on`
   and optional gates. See [getting-started.md](getting-started.md).

Prompts, sidecar files, supervision, retries and parallelism are the library's job.

## Layering (high level → low level)

Three levels, each usable on its own. Most consumers only need the first.

```
DECLARATIVE      a FlowDef (data), or agent_node() -> build_flow()
  │ composes     one node per agent
PRIMITIVES       call run_agent() as the leaf of YOUR OWN flow
  │ uses
ENGINE CORE      run_agent(): spawn + liveness-supervise + kill + sidecar verdict
  │ invokes      runner-agnostic; no Prefect
AGENT RUNTIME    opencode agents (.md) — external, unchanged
```

```python
from agent_flow import FlowDef, NodeDef, run_flow

flow = FlowDef(name="hello", nodes=[
    NodeDef(name="hello", agent="hello-agent",
            inputs={"REPORT": "{run_dir}/hello.md"},
            gate="require_file", gate_args={"path": "{run_dir}/hello.md"}),
])
# without a run_dir, output goes to a temp dir logged at start
run_flow(flow, runtime="opencode")
```

The engine owns the flow logic and dispatches execution to a swappable backend:
in-process by default, or [Prefect](https://www.prefect.io/) via
`--backend prefect` for parallel fan-out, concurrency limits and a run UI. See
[backend.md](../design/backend.md).

The engine is async-first. Entry points keep blocking signatures
(`run_flow`, `run_cli`); from inside an event loop use `await arun_flow(...)`.
Your own callables — impls, gates, exports, hooks — may be sync or async. See
[engine.md](../design/engine.md).
