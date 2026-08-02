---
type: Guide
title: Testing your flow
description: Validate a pipeline's WIRING token-free with mock agents — as an integration test, with the mocks kept out of production code.
tags: [agent-flow, testing, mock-agents, integration-test, how-to]
timestamp: 2026-08-02T14:00:00Z
---

# Testing your flow

`--mock-agents` runs your pipeline through the **real engine** — planner, walker,
backend, gates, exports, `require_file`, the bounded jump-backs — with the
opencode subprocess replaced by deterministic Python stand-ins. No tokens, no
model, milliseconds per node. It is how you check that the FLOW is wired
correctly: the gates fire, exports publish, `require_file` retries, a verifier's
re-run jumps back to the right node. It does **not** check the agents' analysis —
that is the agents' job and needs a real run.

For what a mock agent *is* (the contract, the `ctx` tools, the mode vs. runtime
distinction), see the concept doc [mock-agent.md](../design/mock-agent.md). This
guide is about how to **test with them**.

## The principle: a mock is a test fixture, not production code

A mock agent is a **test double for the agent-runtime leaf**. It exists to
validate the flow token-free. So it belongs with your tests — not in the
production module that ships the pipeline.

Do **not** import and register mocks in the module that defines your flow:

```python
# flow.py  — WRONG: production code depends on its test doubles
from myproject import flow_mocks
REGISTRY = FlowRegistry()
flow_mocks.register(REGISTRY)      # every real run loads N stand-ins it never uses
```

Keep the production flow free of them, and register the mocks from the test:

```
src/myproject/flow.py              # PRODUCTION: FlowDef + gates + exports + schemas. No mock imports.
tests/fixtures/flow_mocks.py       # the mock agent behaviours (the doubles)
tests/integration/test_flow.py     # drives run_flow(mock_agents=True), asserts the wiring
```

Running the whole flow through the real engine while it writes real files to disk
is an **integration test**, not a unit test — the only thing "mock" about it is
the runtime leaf. (Test one gate's `(ctx) -> Directive` logic in isolation and
*that* is a unit test.)

## Write the mock behaviours (the fixture)

A mock agent is `(inv, ctx) -> control-envelope`. It reads the node's resolved
work-order inputs via `ctx.input(...)`, may write files with the `ctx` tools, and
returns the same `{status, result?, rerun_required?}` shape a real agent writes to
its sidecar. Make each one thin — it stands in for the agent, it does not
reimplement it.

```python
# tests/fixtures/flow_mocks.py
from agent_flow import AgentInvocation, FlowRegistry
from agent_flow.runners.mock_exec import MockAgentContext

_OUT = "{PRODUCT_REPOS_ROOT}/{PRODUCT_KEY}/output"


def analyst(inv: AgentInvocation, ctx: MockAgentContext) -> dict:
    # write the convention-path report the node's require_file gate checks
    ctx.write_file(f"{_OUT}/report.md", "# report\n\nmock\n")
    return {"status": "ok", "result": {"findings": 0}}


def verifier(inv: AgentInvocation, ctx: MockAgentContext) -> dict:
    # a verifier normally fixes the report in place; it can also signal a re-run.
    # Test-only knobs ride the ENV, never `-p` — see the note below.
    import os

    if os.environ.get("MOCK_RERUN_ONCE") == "analyst":
        marker = "{run_dir}/.rerun_once"
        try:
            seen = ctx.read_file(marker) == "1"
        except FileNotFoundError:
            seen = False
        if not seen:
            ctx.write_file(marker, "1")
            return {"status": "verified", "rerun_required": ["analyst"]}
    return {"status": "verified", "result": {"issues_found": 0}}


def register(registry: FlowRegistry) -> FlowRegistry:
    registry.mock_agent("my-analyst")(analyst)
    registry.mock_agent("my-verifier")(verifier)
    return registry
```

> **Test-only knobs go through the environment, not `-p`.** A mock reads
> work-order **inputs** (`ctx.input`) and its file tools — it never sees the run
> **params** a `-p KEY=VALUE` flag sets. So a "make this branch happen" switch
> (force a re-run, force a not-ready verdict) rides an env var, which keeps the
> knob out of the real work order the agent would otherwise receive.

## Write the integration test

Import the **production** `FlowDef` and its `REGISTRY` (which already carries the
flow's gates/exports/schemas), add the mocks from your fixture, and drive
`run_flow(..., mock_agents=True)` into a temp product tree. Then assert the
outcomes — do not eyeball a table.

```python
# tests/integration/test_flow.py
import pytest
from agent_flow import run_flow
from myproject.flow import FLOW, REGISTRY
from tests.fixtures import flow_mocks


@pytest.fixture(autouse=True)
def mocks():
    # Isolate per test: reset the shared registry's MOCK agents to the defaults.
    # clear_mock_agents() leaves gates/exports/schemas/params untouched.
    REGISTRY.clear_mock_agents()
    flow_mocks.register(REGISTRY)
    yield
    REGISTRY.clear_mock_agents()


def _run(tmp_path, **kw):
    products = tmp_path / "products"
    (products / "prod" / "output").mkdir(parents=True, exist_ok=True)
    return run_flow(
        FLOW,
        registry=REGISTRY,
        run_dir=str(tmp_path / "rundir"),
        product_key="prod",
        product_repos_root=str(products),
        mock_agents=True,
        **kw,
    )


def test_full_flow_all_nodes_ok(tmp_path):
    out = _run(tmp_path)                       # {node_name: NodeOutcome}
    assert all(o.status in ("ok", "verified") for o in out.values())
    assert (tmp_path / "products" / "prod" / "output" / "report.md").exists()


def test_require_file_retries_when_report_missing(tmp_path):
    # analyst reports ok but writes NO file on the first attempt -> the gate retries.
    calls = {"n": 0}

    def flaky(inv, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "ok"}            # no file -> require_file re-runs it
        return flow_mocks.analyst(inv, ctx)

    REGISTRY.mock_agent("my-analyst")(flaky)
    out = _run(tmp_path)
    assert out["analyst"].status == "ok"
    assert calls["n"] >= 2                      # first attempt + the retry that wrote it


def test_verifier_rerun_jumps_back_bounded(tmp_path, monkeypatch):
    # verifier signals a re-run -> jump back to `analyst`, bounded by max_cycles.
    monkeypatch.setenv("MOCK_RERUN_ONCE", "analyst")
    runs = {"n": 0}

    def counted(inv, ctx):
        runs["n"] += 1
        return flow_mocks.analyst(inv, ctx)

    REGISTRY.mock_agent("my-analyst")(counted)
    out = _run(tmp_path)
    assert out["analyst-verify"].status in ("ok", "verified")
    assert runs["n"] == 2                       # initial + one re-run, then it settles
```

`run_flow` is the blocking entry (it wraps `anyio.run`), so the test calls it
directly — no async test machinery. It returns `{node_name: NodeOutcome}`; assert
on `.status` and on the artifacts the run wrote to `tmp_path`.

## What to assert

Cover the flow's control paths — these are exactly what a real run's cost makes
expensive to discover:

- **The whole DAG runs green** and produces every expected artifact.
- **`require_file` retries** when a node reports ok but writes nothing.
- **A verifier's `rerun_on_signal`** jumps back to its target and settles within
  `max_cycles`.
- **A final check's `rerun_on_named`** re-runs only the node(s) it names — and, for
  a node inside a **parallel group**, that it re-runs *only that node*, not its
  siblings (count the calls per agent to prove it).
- **Exports publish**: a value a readiness node exports is available to a later
  node's work order (assert the run reaches the end, or read the exporting node's
  sidecar under `run_dir`).

## Isolate state between tests

The registry is shared, so reset the **mock agents** between tests (the fixture
above). Use `clear_mock_agents()` — the public reset — rather than reaching into
the private store; it removes only the mock behaviours and leaves the flow's
gates, exports, schemas, params, impls, renderers, and hooks intact. A single
test then overrides one agent *after* the fixture runs, before calling the flow.

## Examples ship their mocks; production pipelines do not

There is one deliberate exception to "mocks live in `tests/`": a **teaching
example** whose whole purpose is to *demonstrate* a token-free run keeps its mock
registration inline — the registration is part of the lesson a reader copies (see
`examples/declarative.py`). A **production pipeline** is not a demonstration; its
mocks are test scaffolding and belong in `tests/`. The rule is the artifact's
intent: *demonstrate* → mocks with it; *run for real* → mocks in the test.
