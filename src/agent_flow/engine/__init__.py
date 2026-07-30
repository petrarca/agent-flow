"""Declaration-driven engine — compiles a Node graph into a runnable flow.

This is the library's Tier-3 API: declare a pipeline as a list of `Node`s and
`build_flow` returns a runnable flow callable that walks the DAG, fans out
parallel groups, invokes each node's gate, and interprets the returned directive
(Continue / Restart / GoTo / Stop) with bounded re-run cycles and per-node
criticality. Execution is dispatched to a `FlowBackend` (the in-process backend
by default, Prefect opt-in); the engine itself owns the flow logic and is
backend-free.

The engine is deliberately AGNOSTIC to what a node does. It knows nothing about
"analysts", "verifiers", reports, or prompts — a `Node` carries a `run` callable
that performs the actual work (build a prompt, call `run_agent`, run a composite
of several agents, whatever). The engine only orchestrates: order, parallelism,
gate directives, criticality. Domain knowledge lives in `run` and `gate`.

Two layers of use:
  - Tier 3 (this package): declare Nodes, call `build_flow`.
  - Tier 2 (not this package): call `run_agent` directly as the leaf of your own
    flow. `build_flow` is optional, not required.

Module map — each stage of the compile-and-walk pipeline is one module:
  dispatch.py     `maybe_await`, the single sync/async dispatch point
  planner.py      `plan_groups` — a flat Node list to ordered parallel groups
  interpreter.py  `interpret` — run ONE node, apply its gate, settle an outcome
  walker.py       `_walk` — movement BETWEEN groups: entry point, jump-back
  builder.py      `build_flow` — validate, plan, resolve a backend, return a flow

The DAG walk and gate interpretation are pure helpers, so the orchestration
logic is unit-testable in-process with no execution backend.

The DAG VOCABULARY (`Node`, `RunContext`, `NodeOutcome`, `NodeBlocked`) lives in
`agent_flow.flow_types`, not here: the backends need those types for their
signatures, and a backend must not depend on the engine it is swappable for.
They are re-exported below so `from agent_flow.engine import Node` keeps working.
"""

from __future__ import annotations

from agent_flow.engine.builder import build_flow
from agent_flow.engine.dispatch import maybe_await
from agent_flow.engine.interpreter import interpret
from agent_flow.engine.planner import plan_groups

# Re-exported for the test suite, which exercises the walk primitives directly.
from agent_flow.engine.walker import _resolve_entry as _resolve_entry
from agent_flow.engine.walker import _resolve_only_index as _resolve_only_index
from agent_flow.engine.walker import _resolve_start_index as _resolve_start_index
from agent_flow.engine.walker import _walk as _walk
from agent_flow.flow_types import DEFAULT_MAX_CYCLES, Criticality, Node, NodeBlocked, NodeOutcome, RunContext, RunFn

__all__ = [
    "DEFAULT_MAX_CYCLES",
    "Criticality",
    "Node",
    "NodeBlocked",
    "NodeOutcome",
    "RunContext",
    "RunFn",
    "build_flow",
    "interpret",
    "maybe_await",
    "plan_groups",
]
