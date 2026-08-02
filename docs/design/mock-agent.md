---
type: Concept
title: Mock agent — a deterministic stand-in for an agent, via a substitution mode
description: The mock_agent seam — a programmable, no-token stub that simulates the STRUCTURED interface of a real agent, enabled by the --mock-agents substitution MODE (not a runtime), driven by structured inputs and producing a control envelope plus optional file side-effects.
tags: [agent-flow, mock-agent, mock-agents, mode, executor, testing, examples]
timestamp: 2026-07-26T00:00:00Z
---

# Mock agent: `mock_agent`

A `mock_agent` is a deterministic, programmable stand-in for a real agent,
enabled by the substitution mode `--mock-agents` (`mock_agents=True`). With the
mode on, any node whose `agent` has a registered `mock_agent` runs via
`MockExecutor` instead of its normal executor: no tokens, no model, but the same
observable contract the engine depends on — structured inputs in, a control
envelope out, and optionally a file (in-memory by default, or on disk) a gate can check.

It does not re-implement an LLM. A real agent reasons over prose; a mock cannot.
A mock simulates only the structured interface around that reasoning: the work
order the flow author wired, the result other nodes consume, and the file
side-effects gates observe.

## Mock is a mode, not a runtime

`runtime` names a real out-of-process runner — `opencode`, `claude`, `codex`.
A mock is not one, so it does not belong on that axis. `--mock-agents` is an
orthogonal switch that substitutes the mock behaviour for whatever a node would
otherwise do, subprocess or in-process.

This also settles what a mock *is*: standing in for execution regardless of how
that execution would happen makes it a mode layered over the runtime axis, never
a value within it. `RUNNERS` holds only real subprocess runners, and
`get_executor(runtime)` keeps its honest meaning.

## What it simulates

| Real runtime agent | mock_agent equivalent |
|---|---|
| receives a structured work order | `ctx.input(key)` |
| writes files | `ctx.write_file(path, content)` |
| reads files | `ctx.read_file(path)` |
| writes a control sidecar | the return value — a control envelope dict |
| reasons over prompt / context / instructions | not simulated |

That structured surface is exactly what gates and `exports` depend on, so a flow
behaves identically whether a node ran real opencode or a mock.

## The contract

```python
from agent_flow import AgentInvocation
from agent_flow.runners.mock_exec import MockAgentContext

@REGISTRY.mock_agent("tech-stack-analyst")
def _tech_stack(inv: AgentInvocation, ctx: MockAgentContext) -> dict:
    product = ctx.input("PRODUCT_KEY", default="unknown")
    ctx.write_file("{run_dir}/tech-stack.md", f"# Tech Stack — {product}\n...")
    return {"status": "ok", "result": {"languages": ["Python", "TypeScript"]}}
```

`mock_agent(inv, ctx) -> control_envelope`, two explicit parameters:

- `inv: AgentInvocation` — the same neutral invocation the other executors get.
  Fully available, but the prompt and instructions are diagnostic only: a mock
  may log them, it does not drive behaviour off prose. Behaviour comes from
  structured inputs via `ctx`.
- `ctx: MockAgentContext` — the mock's tools. Primitives only, zero policy.

### The tools

| Tool | Purpose |
|---|---|
| `input(key, default=None)` | read a structured work-order input, already templated |
| `write_file(path, content)` | write a file; the checkable side-effect |
| `read_file(path)` | read a file (a verifier or extractor reading upstream output) |

Deliberately absent: no `write_report` or other domain helper (that would bake
policy back into the library); no `edit`/`append` (it composes from read + write);
no `exists`/`list_dir` until a concrete example needs them.

`result` and `rerun_required` are not tools — they are fields of the returned
envelope, mirroring how a real agent puts them in its sidecar.

### In-memory filesystem (the default for a mock run)

`write_file`/`read_file` resolve a path with the same strict `{param}` templating
a gate uses, then act on it — but a resolved path is a **`UPath`**, not a
`pathlib.Path`. When the run's `run_dir` (and the pipeline's own anchors) carry a
`memory://` scheme, the write lands in an **in-memory filesystem**; when they are
plain/local, it is an ordinary on-disk `Path`. One resolution serves both.

A `mock_agents=True` run with **no explicit `run_dir`** defaults its `run_dir` to a
unique `memory://run-<id>/` root, so the mock's artifacts, the control sidecar, and
the `require_file` gate's reads all resolve in memory — the run touches no disk and
a flow test is a **unit test**. This works precisely because every disk-touching
site in the mock path (`MockAgentContext`, the `MockExecutor` sidecar write, and
`gates.produced`) uses the `pathlib` API, which a `UPath` satisfies unchanged. The
seam is bounded to this path: a real subprocess writes real disk (you cannot fake a
filesystem out from under an external process), so the in-memory FS is for the
mock/in-process world only. See the [testing guide](../usage/testing.md#in-memory-runs-integration-test-to-unit-test).

## Two layers

- **The behaviour** (`mock_agent(inv, ctx) -> envelope`) is pure. It reads input,
  optionally writes a file, returns an envelope. It knows nothing about control
  files or which executor invoked it.
- **The MockRuntime** (`MockExecutor`) is the complement to opencode: it
  surrounds the behaviour with what a real out-of-process runner does, above all
  writing the control sidecar to disk and assembling the runner-shaped
  `AgentResult`.

Keeping sidecar mechanics entirely in the outer layer is what lets one behaviour
serve both seams unchanged.

## The mode

> With `mock_agents` on: a node whose `agent` has a registered `mock_agent` runs
> via `MockExecutor`. Otherwise the node runs its normal executor.

The fallback enables **partial mocking** — mock the expensive or flaky nodes, run
the rest for real. Stated plainly: a `--mock-agents` run can still spawn opencode
for any node you did not mock. The mode substitutes where a mock exists; it does
not force determinism everywhere.

Because it substitutes regardless of how a node would otherwise run, it covers
both seams uniformly, and a registered `mock_agent` wins over a node's `impl`.
One registration, one flag, both seams.

### Where it plugs in

Executor selection lives in the node's `run` callable, with the mode check ahead
of the `impl` decision:

```
node.run:
  behaviour = registry.get_mock_agent(node.agent) if (mock_agents and registry
              and registry.has_mock_agent(node.agent)) else None
  if behaviour is not None:
      executor = MockExecutor(behaviour, work_order=resolved_inputs, tmpl=tmpl)
  elif impl is not None:
      executor = InProcessExecutor(impl)
  else:
      executor = get_executor(runtime)
```

`engine/` contains no reference to `runtime`, mock, or any executor type. The
engine knows only `Node.run(ctx) -> dict`, gate directives, criticality and
jump-back, so gates, `exports` and re-runs cannot tell a mock run from a real one.

### A sibling executor

`MockExecutor` implements the `AgentExecutor` seam, as a sibling of the others —
not a subclass:

```
AgentExecutor (ABC)        shared: control-dict -> AgentResult, schema validation, status policy
├── SubprocessExecutor     spawns + supervises a process; COMPOSES an AgentRunner
├── InProcessExecutor      calls impl(inv) -> typed value; no subprocess
└── MockExecutor           calls mock_agent(inv, ctx) -> envelope; writes the sidecar
```

Subclassing `SubprocessExecutor` would drag in the spawn/supervise/kill machinery
a mock cannot use. (Same precedent as opencode itself, which is an `AgentRunner`
that `SubprocessExecutor` composes, not an executor subclass.) What they do share
— control dict to `AgentResult`, schema validation, status policy — lives on the
ABC base.

Against `InProcessExecutor`, which also calls a Python callable: the behaviour is
resolved by agent name, returns a control envelope rather than a typed value,
receives the `ctx` tools, and gets a sidecar written for it. A non-dict return is
rejected; `None` becomes a bare `ok`.

### Registration

```python
@REGISTRY.mock_agent("name")
def behaviour(inv, ctx): ...
```

Same by-name pattern as gates, exports and `agent_impl`, on the `FlowRegistry`
that `run_cli` / `run_flow` already thread. The only new surface is the mode flag
and the `node_builder` check.

## Why `runtime` stays runner-only

A node's execution model is decided by one thing: does it have an `impl`?

- With an `impl`, the node runs in-process. What lives inside that callable —
  PydanticAI, LangChain, plain heuristics — is your code's business; there is
  nothing for the library to select, and no such thing as a "PydanticAI runtime".
- Without one, an out-of-process runner runs it, and that is what `runtime`
  selects.

So `runtime` is not overloaded: it is the runner selector for the no-`impl`
branch, naming only real runners.

The contract shapes stay distinct on purpose. `mock_agent` returns a control
envelope because it impersonates a runner; `agent_impl` returns a typed value
because it genuinely is the agent and has no sidecar.

For an in-process node there is also a simpler option that involves no mock at
all: register a different `impl`. Plain dependency injection. Use `--mock-agents`
when you want one registration to serve uniformly across seams.

## The surviving subprocess stub

A minimal, domain-free subprocess stub exists only to test `SubprocessExecutor`'s
spawn / supervise-by-liveness / kill-on-stale path — the one thing an in-process
mock cannot exercise, because that machinery exists precisely *because* the real
runtime is a subprocess. It takes no agent names and holds no domain logic: it
either sleeps (to test the kill path) or emits a caller-supplied envelope
(`--emit`). A dumb opencode-shaped process, not a simulator of any agent.
