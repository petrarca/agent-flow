---
type: Guide
title: Testing your flow
description: Validate a pipeline's WIRING token-free with mock agents — a fast in-memory unit test by default (on-disk when you want inspectable artifacts), with the mocks kept out of production code.
tags: [agent-flow, testing, mock-agents, unit-test, in-memory, how-to]
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

By default a mock run writes to an **in-memory filesystem** (see [In-memory
runs](#in-memory-runs-integration-test-to-unit-test) below), so the whole flow
through the real engine is a fast, hermetic **unit test** — no disk, no
`tmp_path`. Point the flow's paths at a real directory instead and the same test
becomes an integration test that leaves inspectable artifacts. Either way the only
thing "mock" about it is the runtime leaf. (Testing one gate's `(ctx) -> Directive`
logic in isolation is a smaller unit test still.)

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

# Paths template against, in precedence order: the node's work-order INPUTS
# (uppercase here, the keys `ctx.input` exposes), the run PARAMS, then {run_dir}.
# Resolution is STRICT — an unknown placeholder raises — so a key you use here
# must actually be wired on the node (`inputs={"PRODUCT_KEY": "{product_key}", …}`).
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
            return {"status": "verified", "rerun_required": True}
    return {"status": "verified", "result": {"issues_found": 0}}


def register(registry: FlowRegistry) -> FlowRegistry:
    registry.mock_agent("my-analyst")(analyst)     # keyed by AGENT name
    registry.mock_agent("my-verifier")(verifier)
    return registry
```

> **Register by AGENT name; assert by NODE name.** A mock is matched to the
> node's `agent` (`NodeDef(name="analyst", agent="my-analyst", …)`), so you
> register `"my-analyst"` — but `run_flow` returns outcomes keyed by the **node**
> name, so you assert `out["analyst"]`. They are often different, and a mock that
> silently never runs is usually a name mismatch. `registry.mock_agents()` lists
> what is registered if you need to check.

> **Test-only knobs go through the environment, not `-p`.** A mock reads
> work-order **inputs** (`ctx.input`) and its file tools — it never sees the run
> **params** a `-p KEY=VALUE` flag sets. So a "make this branch happen" switch
> (force a re-run, force a not-ready verdict) rides an env var, which keeps the
> knob out of the real work order the agent would otherwise receive.

## Write the test

Import the **production** `FlowDef` and its `REGISTRY` (which already carries the
flow's gates/exports/schemas), add the mocks from your fixture, and drive
`run_flow(..., mock_agents=True)`. Then assert the outcomes — do not eyeball a
table.

```python
# tests/unit/test_flow.py  (in-memory -> a unit test; see below)
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


def _run(**kw):
    # No run_dir + mock_agents=True -> a hermetic memory:// run (no disk). Point
    # the pipeline's OWN path anchors at the same memory root so the artifacts a
    # mock writes and the require_file gate checks resolve in memory too.
    return run_flow(
        FLOW,
        registry=REGISTRY,
        product_key="prod",
        product_repos_root="memory://run/products",
        mock_agents=True,
        **kw,
    )


def test_full_flow_all_nodes_ok():
    out = _run()                               # {node_name: NodeOutcome}
    assert all(o.status in ("ok", "verified") for o in out.values())


def test_require_file_retries_when_report_missing():
    # analyst reports ok but writes NO file on the first attempt -> the gate retries.
    calls = {"n": 0}

    def flaky(inv, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            return {"status": "ok"}            # no file -> require_file re-runs it
        return flow_mocks.analyst(inv, ctx)

    REGISTRY.mock_agent("my-analyst")(flaky)
    out = _run()
    assert out["analyst"].status == "ok"
    assert calls["n"] >= 2                      # first attempt + the retry that wrote it


def test_verifier_rerun_jumps_back_bounded(monkeypatch):
    # verifier signals a re-run -> jump back to `analyst`, bounded by max_cycles.
    monkeypatch.setenv("MOCK_RERUN_ONCE", "analyst")
    runs = {"n": 0}

    def counted(inv, ctx):
        runs["n"] += 1
        return flow_mocks.analyst(inv, ctx)

    REGISTRY.mock_agent("my-analyst")(counted)
    out = _run()
    assert out["analyst-verify"].status in ("ok", "verified")
    assert runs["n"] == 2                       # initial + one re-run, then it settles
```

`run_flow` is the blocking entry (it wraps `anyio.run`), so the test calls it
directly — no async test machinery. It returns `{node_name: NodeOutcome}`; assert
on `.status`. To assert on a written artifact in a memory run, read it back with a
`UPath`: `from upath import UPath; UPath("memory://run/products/prod/report.md").read_text()`.

Two import assumptions in that file: your project package is importable (a `src/`
layout installed editable, e.g. `uv pip install -e .`), and `tests.fixtures`
resolves because pytest puts the rootdir on `sys.path` — so **run these under
pytest**, not by executing the file directly. If your layout differs, insert the
paths explicitly at the top of the test instead:

```python
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tests"))   # then: from fixtures import flow_mocks
```

A default (in-memory) mock flow test is a **unit test** — token-free, disk-free,
milliseconds — so it can live under `tests/unit/`. Keep the on-disk variant (next
section) under `tests/integration/` if you use one, or mark it
`@pytest.mark.integration`.

## In-memory runs: integration test to unit test

By default a mock run (`mock_agents=True`) with **no explicit `run_dir`** uses an
**in-memory filesystem**: the `run_dir` is a unique `memory://run-<id>/` root, and
the mock's writes, its control sidecar, and the `require_file` gate's reads all
resolve there — nothing touches disk. That is what turns a mock flow test from an
integration test into a **unit test**. It works because a `run_dir` is a
`pathlib`-compatible path either way (a local `Path` or a `UPath` over the
in-memory FS), so no gate, mock, or executor code changes.

One thing you control: a pipeline usually writes artifacts to paths **anchored
outside `run_dir`** — e.g. `{product_repos_root}/{product_key}/report.md`. The
library never rewrites your params, so for a fully in-memory run you point those
anchors at the same memory FS yourself:

```python
run_flow(FLOW, registry=REGISTRY, mock_agents=True,
         product_key="prod",
         product_repos_root="memory://run/products")   # anchor in memory too
```

Now the mock's `ctx.write_file("{product_repos_root}/{product_key}/report.md", …)`
and the node's `gate_args={"path": "{product_repos_root}/…/report.md"}` both land
in the in-memory tree. Isolation is per **netloc**: give each run its own
`memory://<name>/…` and their trees never collide (the underlying memory store is
process-global, so the netloc *is* the boundary — don't share one across tests
that must stay independent).

**The on-disk escape hatch.** Pass an explicit local `run_dir=` (and local
anchors) and the mock run writes real files you can inspect afterwards — the
former default. Use it when you *want* the artifacts on disk:

```python
def _run(tmp_path, **kw):
    products = tmp_path / "products"
    (products / "prod").mkdir(parents=True, exist_ok=True)
    return run_flow(FLOW, registry=REGISTRY, mock_agents=True,
                    run_dir=str(tmp_path / "rundir"),
                    product_key="prod", product_repos_root=str(products), **kw)
```

This is a real behavior note: a default mock run no longer leaves files in a temp
dir — they live in memory and vanish when the process ends. Pass an explicit
`run_dir=` to keep them.

## What to assert

Cover the flow's control paths — these are exactly what a real run's cost makes
expensive to discover:

- **The whole DAG runs green** and produces every expected artifact.
- **`require_file` retries** when a node reports ok but writes nothing.
- **A granted re-run** (`rerun_targets`) jumps back to its target and settles
  within `max_cycles`.
- **A multi-target grant** re-runs only the node the agent names — and, for a node
  inside a **parallel group**, that it re-runs *only that node*, not its siblings
  (count the calls per agent to prove it). Naming the GROUP re-runs every member.
- **Exports publish**: a value a readiness node exports is available to a later
  node's work order (assert the run reaches the end, or read the exporting node's
  sidecar under `run_dir`).

## Watching a run by hand

Tests assert; sometimes you want to *watch*. If your flow ships a `run_cli`
entry point, the same mocks drive an interactive run — useful while building a
pipeline up node by node:

```bash
python flow.py run --mock-agents --show-events      # the whole flow, token-free
python flow.py run --mock-agents --only analyst        # just one node/group
python flow.py run --mock-agents --start-from verify   # from a node to the end
python flow.py run --mock-agents --stop-after synth     # up to and including a node
python flow.py run --mock-agents --start-from a --stop-after b  # the a..b segment
```

That requires the mocks to be registered on the registry `run_cli` uses — which,
by the rule above, production is not. Either add a tiny test-only entry point that
registers the fixture and calls `run_cli`, or keep the manual mode for the
examples and rely on the assert-based tests for the pipeline. The tests are the
contract; this is a development convenience.

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
