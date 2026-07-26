---
type: Concept
title: Mock agent — a deterministic stand-in for an agent, via a substitution mode
description: The mock_agent seam — a programmable, no-token stub that simulates the STRUCTURED interface of a real agent, enabled by the --mock-agents substitution MODE (not a runtime), driven by structured inputs and producing a control envelope plus optional file side-effects.
tags: [agent-flow, mock-agent, mock-agents, mode, executor, testing, examples]
timestamp: 2026-07-26T00:00:00Z
---

# Mock agent: `mock_agent`

A `mock_agent` is a **deterministic, programmable stand-in for a real agent**,
enabled by a substitution **mode** (`--mock-agents` / `mock_agents=True`). When
the mode is on, any node whose `agent` has a registered `mock_agent` runs via the
`MockExecutor` instead of its normal executor — driving a flow with **no tokens
and no model** while faithfully reproducing the *observable contract* the engine
depends on: structured inputs in, a control envelope out, and optionally a file on
disk that a downstream gate can check.

**Mock is a mode, not a runtime.** This is the central design decision. `runtime`
names a *real* out-of-process agent runner — `opencode`, `claude`, `codex` — and
nothing else. A mock is not a real runner, so it does not belong in that axis.
Instead, `--mock-agents` is an orthogonal switch that *substitutes* the mock
behaviour for whatever a node would otherwise do (subprocess **or** in-process).
See [The mock mode](#the-mock-mode).

It is emphatically **not** an attempt to re-implement an LLM. A real agent reasons
over natural-language prompt/context/instructions; a mock cannot and does not. A
mock simulates only the **structured interface** around that reasoning: the
machine-readable work order the flow author wired, the structured result other
nodes consume, and the file side-effects gates observe.

## Why this exists

The previous mock was a subprocess: `MockRunner` spawned a packaged
`_mock_agent.py`, a script that impersonated `opencode run --agent X` — it read
the prompt from argv, wrote a control sidecar, and exited. Two problems:

1. **Domain agents were baked into the library.** A subprocess cannot see the
   parent's registry or any Python closure — it only receives strings on argv.
   So the only way the stub could know what "tech-stack-analyst" should do was a
   hardcoded switch *inside a library file* (`DISPATCH` + `-analyst`/`-verifier`
   suffix routing). That is a layering violation: the library is supposed to know
   execution mechanics, never what an agent *does*.
2. **The behavior was not programmable.** An example or test could not define a
   new simulated agent — or an arbitrary producer -> consumer handoff — without
   editing the library.

3. **Mock was miscast as a runtime.** `--runtime mock` selected `MockRunner`, as
   if a mock were a peer of opencode. But `runtime` should mean "which *real*
   runner", and a mock is not one. Treating it as a runtime value forced it
   through `SubprocessExecutor` (the only thing runtimes resolved to) — the very
   reason it had to be a subprocess.

The insight that resolves all three: **a mock does not need to be a subprocess,
and it is not a runtime — it is a substitution *mode*.** The whole point of a mock
is to *stand in for* the real execution, so spawning a process that does no model
work — only to write a file the parent then reads — is pure ceremony. Run the
behavior **in-process** and the argv boundary disappears; the behavior can then be
a **flow-supplied Python callable**, resolved by name from the registry, exactly
like gates, exports, schemas, and in-process `agent_impl`s. And because it stands
in for execution regardless of *how* that execution would otherwise happen, it is
naturally a **mode** (`--mock-agents`) layered over the honest `runtime` axis, not
a value within it.

## What it simulates (and what it does not)

A `mock_agent` reproduces the parts of a real agent the **engine actually
observes**:

| Real runtime agent | mock_agent equivalent |
|---|---|
| receives a structured work order (`KEY: value` inputs the flow wired) | `ctx.input(key)` — the **structured** input |
| writes files (its report, arbitrary outputs) | `ctx.write_file(path, content)` |
| reads files (a verifier reading the upstream report) | `ctx.read_file(path)` |
| writes a control sidecar (`status` / `result` / `rerun_required`) | the **return value** — a control envelope dict |
| reasons over prompt / context / instructions | **not simulated** — cannot rebuild an LLM |

The mock keys off **structured inputs** — used when it needs to construct
something, or flow a value into its output — and it can **write a file**, because
some real agents do and **downstream nodes or gates check for it** (e.g.
`require_file`, or a verifier that reads it). That structured surface — input in,
structured result out, maybe a file — is precisely what gates and `exports`
depend on, so the flow behaves identically whether a node ran real opencode or a
mock.

## The contract

```python
from agent_flow import AgentInvocation
from agent_flow.runners.mock_exec import MockAgentContext  # tools

@REGISTRY.mock_agent("tech-stack-analyst")
def _tech_stack(inv: AgentInvocation, ctx: MockAgentContext) -> dict:
    product = ctx.input("PRODUCT_KEY", default="unknown")
    ctx.write_file("{run_dir}/tech-stack.md", f"# Tech Stack — {product}\n...")
    return {"status": "ok", "result": {"languages": ["Python", "TypeScript"]}}
```

### Signature: `mock_agent(inv, ctx) -> control_envelope`

Two explicit parameters — no signature sniffing:

- **`inv: AgentInvocation`** — the full neutral invocation, identical to what
  `SubprocessExecutor` and `InProcessExecutor` receive (`agent`, `run_dir`,
  `model`, `result_schema`, `instructions`, `shared_instructions`,
  `shared_context`, the composed `prompt`, ...). It is **fully available**, but
  the prompt / context / instructions are **diagnostic only** — a mock may
  inspect them for logging or explanation, but it does not drive behavior off
  prose (that would be pretending to reason). Behavior is driven by structured
  inputs via `ctx`.
- **`ctx: MockAgentContext`** — the mock's "tools": the simulated counterpart of
  a real agent's toolset. Primitives only, **zero policy**.

### The tools (`MockAgentContext`)

Derived strictly from what the example agents actually do (analyst writes a
report; verifier reads + appends; extractor reads + writes JSON; summary writes):

| Tool | Purpose |
|---|---|
| `input(key, default=None)` | read a **structured** work-order input (the resolved `inputs={...}` value, with `{run_dir}`/`{param}` already templated). |
| `write_file(path, content)` | write a file; the checkable side-effect. Accepts `{run_dir}`/`{param}` templating. |
| `read_file(path)` | read a file (a mock verifier/extractor reading upstream output). Accepts templating. |

Deliberately **absent**:

- **No `write_report` or any domain helper.** That would bake policy ("there is a
  REPORT input; write there") back into the library — the exact smell being
  removed. The mock, like a real agent, decides *what* file to write and *what*
  to call it.
- **No `edit`/`append` tool.** Edit composes from read + write:
  `ctx.write_file(p, ctx.read_file(p) + note)`. A verifier mock uses that.
- **No `exists` / `list_dir`.** No example agent needs them; adding them would be
  speculative. Add when a concrete example requires it.

The `result` payload and the `rerun_required` signal are **not** tools — they are
fields of the **returned control envelope**, mirroring how a real agent puts them
in its sidecar.

### The return value: a control envelope

The hook returns the same envelope a real agent writes to its sidecar (see
[control-file](control-file.md)):

```python
{"status": "ok"}                                   # minimal
{"status": "ok", "result": {"languages": [...]}}   # + structured result (exports read result.*)
{"status": "verified", "rerun_required": ["tech-stack"]}  # signal a jump-back
```

The behavior only *returns* the envelope. **Persisting it as a control file on
disk is the MockRuntime's job, not the behavior's** — the same division as with a
real runner, where the agent produces the verdict and the runtime materializes the
sidecar. `MockExecutor` writes the returned envelope to a real control sidecar
under `run_dir` (same path/shape opencode would produce), then assembles the
`AgentResult` (control envelope + telemetry zeros), validating `result_obj`
against the invocation's `result_schema` when one is present — identical
post-processing to the other executors. Gates then read `ctx.obj`, `exports` map
`result.*` into the run-context for downstream nodes, and `rerun_required` drives
the bounded jump-back — all unchanged.

## Two layers: clean behavior vs. opencode-complementary MockRuntime

The design deliberately separates two layers, and only the inner one is a
candidate for reuse elsewhere:

- **Inner layer — the behavior** (`mock_agent(inv, ctx) -> envelope`). Pure: it
  reads structured input, optionally writes a file via `ctx`, and returns a
  control envelope. It knows **nothing** about control files, sidecars, or which
  executor invoked it. `ctx` offers *agent tools* (`input`/`write_file`/
  `read_file`), never *runtime mechanics*.

- **Outer layer — the MockRuntime** (`MockExecutor`). The **complement to
  opencode**: it surrounds the behavior with what a real out-of-process runner
  does — most importantly, **writing the control file/sidecar to disk** and
  assembling the runner-shaped `AgentResult`. An in-process agent has no such
  surrounding (no sidecar, no control-file mechanics), which is exactly why this
  layer is out-of-process-only.

Keeping the sidecar mechanics entirely in the MockRuntime (never in the behavior
contract or `ctx`) is what keeps the interface clean: the same inner behavior
could later be routed to a thinner, in-process wrapper that simply skips the
control-file step. That would be a **routing decision, not a contract change** —
see "Later: reuse for in-process" below.

## The mock mode

Mock is enabled by a run-level switch, **`--mock-agents`** (CLI) /
`mock_agents=True` (run param), orthogonal to `runtime`. The rule is one line:

> When `mock_agents` is on: for any node whose `agent` has a registered
> `mock_agent`, execute it via `MockExecutor`. Otherwise, the node runs its
> **normal** executor (subprocess or in-process).

That fallback is deliberate — it enables **partial mocking**: mock the expensive
or flaky nodes, run the rest for real. A consequence worth stating plainly: a
`--mock-agents` run **can still spawn opencode** for any node that has no
registered `mock_agent`. Mock mode substitutes where a mock exists; it does not
force determinism on nodes you did not mock.

Because the mode substitutes execution regardless of *how* a node would otherwise
run, it covers **both** seams uniformly — a subprocess node and (later) an
in-process node are intercepted the same way, by the same registered `mock_agent`.
This is the DX win: one registration, one flag, both seams. (In-process
interception is out of scope for the initial implementation — see "Later: reuse
for in-process" — but the mode is the mechanism that will carry it.)

### Where it plugs in

Executor selection happens inside the node's `run` callable (`node_builder.py`),
where today the choice is `InProcessExecutor(impl)` if a node has an `impl`, else
`get_executor(runtime)`. The mock mode adds a **prior** check, ahead of that
decision:

```
node.run:
  if mock_agents and registry.has_mock_agent(node.agent):
      executor = MockExecutor(registry)          # substitute, regardless of impl/runtime
  elif impl is not None:
      executor = InProcessExecutor(impl)          # normal in-process
  else:
      executor = get_executor(runtime)            # normal subprocess: opencode / claude / codex
```

Two clean-ups follow from mock no longer being a runtime:

- **`mock` leaves the `RUNNERS` dict** and `get_executor` no longer knows the
  string `"mock"`. `RUNNERS` becomes purely *real* subprocess runners
  (`opencode`, `claude`, `codex`); `get_executor(runtime)` goes back to its honest
  meaning — "runtime -> `SubprocessExecutor(that runtime's runner)`". The runner
  is an implementation detail of the subprocess executor, never something a mock
  routes through.
- **`MockRunner` is deleted.** The old mock was an `AgentRunner` (a subprocess
  wire adapter, `build_command`/`parse_event`) composed into `SubprocessExecutor`
  — which is what forced it to be a subprocess. It is not a runner anymore.

### The engine never knows a mock exists

The mode check and executor selection live entirely in the node's `run` callable.
**`engine.py` contains no reference to `runtime`, mock, or any executor type**
(verified). The engine knows only `Node.run(ctx) -> dict`, gate directives,
criticality, and jump-back — it operates on the resulting `AgentResult`
identically regardless of what produced it. `MockExecutor` is therefore invisible
above the executor seam: gates, `exports`, and the bounded jump-back cannot tell a
mock run from a real one.

### `MockExecutor` is a sibling `AgentExecutor`

`MockExecutor` implements the **`AgentExecutor`** seam (`runners/executor.py`,
`run(inv) -> AgentResult`) — the same seam `SubprocessExecutor` and
`InProcessExecutor` implement. It is a **sibling** of them at the ABC level, not a
subclass of either:

- It is **not** a `SubprocessExecutor` subclass. `SubprocessExecutor`'s defining
  behavior — always `Popen` a fresh process, supervise it by liveness, kill on
  stale — is exactly what a mock lacks; subclassing would drag in machinery it
  cannot use. (Note the precedent: opencode itself is **not** an executor subclass
  either — it is an `AgentRunner` that `SubprocessExecutor` *composes*. Runtimes
  plug in by composition, not inheritance.)
- What `MockExecutor` and `SubprocessExecutor` genuinely **share** is only the
  *result-assembly tail*: turn a control dict into `AgentResult`, validate
  `result_obj` against the schema, apply the status->exception policy. That shared
  tail belongs on the **`AgentExecutor` ABC base** (the ABC docstring already
  anticipates this), not inherited from `SubprocessExecutor`.

```
AgentExecutor (ABC)               shared: control-dict -> AgentResult, schema validation, status policy
├── SubprocessExecutor            spawns + supervises a process; COMPOSES an AgentRunner (opencode/claude)
├── InProcessExecutor             calls an impl(inv) -> typed value; no subprocess
└── MockExecutor  (NEW)           calls mock_agent(inv, ctx) -> envelope; writes the sidecar; no subprocess
```

The defining difference from `InProcessExecutor` (both call a Python callable
without a subprocess): `MockExecutor`'s resolution key is `mock_agent`, its return
contract is a **control envelope**, it constructs and passes the **`ctx` tools**,
and it performs the out-of-process runner's surrounding by **writing the control
sidecar to disk** (the MockRuntime role, complementary to opencode).

### Registration (mirrors `agent_impl` / gates / exports)

`FlowRegistry` gains the same by-name pattern already used for in-process impls:

```python
@REGISTRY.mock_agent("name")   # decorator
def behavior(inv, ctx): ...
# + get_mock_agent(name) / has_mock_agents()
```

Because behaviors live on the `FlowRegistry` that `run_cli` / `run_flow` already
thread, `--mock-agents` (or `mock_agents=True`) + a registry carrying
`mock_agent`s works through the existing plumbing. The only new surface is the
mode flag itself and the `node_builder` mode-check.

## The two execution models, and why `runtime` is runner-only

A node's **execution model** is decided by one thing: does it have an `impl`?
(`node_builder.py`, the `impl is not None` decision.)

- **You wrote an `impl`** (a Python callable, e.g. a PydanticAI agent) → the node
  runs **in-process**. What framework lives inside that callable — PydanticAI,
  LangChain, plain heuristics — is *your code's business*; there is nothing for
  the library to "select". There is no such thing as a "PydanticAI runtime".
- **You did not** → an **agentic runner** runs it out-of-process, and *that* is
  what `runtime` selects (opencode / Claude Code / mock).

So `runtime` is **not overloaded**: it is the runner selector for the no-`impl`
branch only. It never governs in-process nodes, and it names only *real* runners
(`opencode` / `claude` / `codex`) — **not** mock. Mock is the `--mock-agents`
mode, layered over this axis, not a value within it.

The mode's fallback keeps the two execution models honest. When `--mock-agents`
is on:
- a node with a registered `mock_agent` -> `MockExecutor`;
- a node without one -> its normal executor: `InProcessExecutor(impl)` if it has
  an `impl`, else `get_executor(runtime)` (a real runner).

Contract shapes stay distinct on purpose: `mock_agent` returns a **control
envelope** (it impersonates a runner, and the MockRuntime persists a sidecar);
`agent_impl` returns a **typed pydantic value** (it genuinely is the agent, and
has no sidecar). Both end as deterministic Python, but mixing their contracts
would blur the two roles.

Note there is also a *second*, pre-existing way to avoid an LLM for an in-process
node that does **not** involve mock at all: because an in-process agent **is** an
`impl`, you can simply register a **different `impl`** (a stub) — plain dependency
injection of your own code. The `--mock-agents` mode is the option when you want a
*single* registered `mock_agent` to serve uniformly across seams; impl-swapping is
the option when the node is in-process and you would rather just provide a
different callable.

### Scope: in-process interception deferred

The `--mock-agents` mode is *defined* to cover both seams (subprocess and
in-process), and the two-layer split makes that clean: the **inner behavior**
(`mock_agent(inv, ctx) -> envelope`) is agnostic to how it is invoked; only the
**surrounding** differs — for a subprocess node the MockRuntime writes a sidecar,
for an in-process node a thinner wrapper would skip it.

For the **initial implementation**, only the out-of-process (subprocess) case is
built: `--mock-agents` intercepts no-`impl` nodes. Intercepting in-process
(`impl`) nodes with the same mode is a **routing addition, not a contract change**,
and is deliberately out of scope now. The contract is shaped so it drops in later
without touching `mock_agent`, `MockAgentContext`, or the envelope.

## What is deleted, and the one subprocess piece that survives

- **Deleted:** the subprocess-spawning `MockRunner` (`runners/mock.py`) and the
  hardcoded domain switch in `core/_mock_agent.py` (`DISPATCH`, `_is_tech_analyst`,
  `_is_tech_verifier`, the tech-stack report bodies). **All domain agents leave
  the library.**
- **Survives, but stripped to be domain-free:** a minimal subprocess stub used
  **only** to test `SubprocessExecutor`'s spawn / supervise-by-liveness /
  kill-on-stale path — the one thing an in-process mock cannot exercise, because
  that machinery only exists *because* the real runtime is a subprocess. It takes
  no agent names and holds no domain logic — it either sleeps (to test the kill
  path, the old `MOCK_HANG`) or emits a caller-supplied envelope (`--emit`). It
  is a dumb opencode-shaped process, not a simulator of any particular agent.

## Migration

The example behaviors currently trapped in `_mock_agent.py`
(`tech-stack-analyst`, `domain-analyst`, the verifiers, `executive-summary`, and
the generic `analyst`/`verifier`/`extractor`) move **out of the library** and
into the example/test modules as registered `mock_agent`s. This makes
`imperative.py` / `declarative.py` self-contained: their simulated agents live
next to the flow that uses them, which is the correct home for domain behavior.
Their no-token story is preserved but re-expressed: instead of `--runtime mock`
(gone), they run with `--mock-agents`, which resolves the `MockExecutor`
(MockRuntime) for each node whose agent has a registered `mock_agent`, instead of
spawning a subprocess.

This also unlocks the producer -> consumer example that motivated the redesign: a
node whose `mock_agent` returns a `result` the flow `exports` into the
run-context, consumed by a downstream node as a `{param}` — all self-contained,
deterministic, and token-free.

## Dependency: CLI preflight (issue #11)

A flow fully covered by `mock_agent`s has **no `.opencode/agent/*.md` files** —
its agents are Python behaviors on the registry. The current CLI preflight
defaults `runtime=opencode` and requires an agent-dir (`check_agent_dir_exists`),
which would abort such a flow. The `--mock-agents` mode therefore intersects issue
#11: the preflight must not demand an agent-dir when `--mock-agents` is on and
every un-mocked node (if any) still has its runner available. Until #11 is
addressed, a fully-mocked flow runs programmatically via `run_flow` (which skips
the CLI preflight), as the in-process example already does. (Note: with partial
mocking, un-mocked nodes still need their real runner + agent-dir — the preflight
relaxation applies only to the mocked ones.)

## Prototype status

Design only. Not yet implemented. Scope when built:

- `MockExecutor` + `MockAgentContext` (`runners/mock_exec.py`) — the MockRuntime
  and its tools; the shared result-assembly tail hoisted onto the `AgentExecutor`
  ABC base.
- `FlowRegistry.mock_agent` registration trio (`mock_agent` / `get_mock_agent` /
  `has_mock_agent`).
- The `--mock-agents` / `mock_agents=True` mode: the CLI flag, the run param, and
  the `node_builder` mode-check that routes to `MockExecutor` when a `mock_agent`
  is registered (subprocess nodes only in this phase).
- Removal of `mock` from the runtime axis: delete `MockRunner`, drop `"mock"` from
  `RUNNERS`, and simplify `get_executor` back to "runtime -> subprocess runner".
- The domain-free supervise/kill subprocess stub (replacing the `_mock_agent.py`
  domain switch).
- Migration of the example behaviors to registered `mock_agent`s, and the
  producer -> consumer example.
