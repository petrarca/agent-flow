"""Backend seam — the ABC every execution backend implements.

The engine (`build_flow`) owns all FLOW LOGIC: it plans groups, walks the DAG,
honors jump-backs / start_from / only, installs the run-context, and builds a
`run_node(node_name, run_dir, params) -> NodeOutcome` closure that runs ONE node
(emit start, interpret the gate, stamp duration, emit finish). None of that is
backend-specific.

A backend supplies only the mechanics of EXECUTION:
  1. run a group's nodes — one inline, or N concurrently (the parallel fan-out),
  2. an optional concurrency limit around node execution,
  3. a logger,
  4. bootstrap / teardown lifecycle hooks.

Unlike the runner seam (a `Protocol`, because build_command/parse_event share no
code), backends DO share logic: the solo-vs-parallel branch and the
failure->degraded mapping are identical across backends — only the concurrency
PRIMITIVE differs (a threadpool vs Prefect task submission). So this seam is an
`abc.ABC` with a concrete template-method `run_group` and one abstract primitive
`_execute_parallel`, mirroring cypher-graphdb-core's CypherBackend.
"""

from __future__ import annotations

import abc
from collections.abc import Awaitable, Callable
from typing import Any

from agent_flow.flow_types import Node, NodeOutcome

# A coroutine closure the engine hands the backend: run ONE node to a
# NodeOutcome. It is fully backend-agnostic (it awaits interpret + emits node
# events). The backend only decides WHEN/HOW MANY run concurrently, never what
# running one means.
RunNode = Callable[[str], Awaitable[NodeOutcome]]


class FlowBackend(abc.ABC):
    """Abstract execution backend: how a planned DAG's groups actually run.

    Subclasses implement the varying primitives (`_execute_parallel`,
    `apply_concurrency_limit`, `get_logger`, `bootstrap`, `teardown`); the shared
    group-orchestration (`run_group`) is concrete here so no backend re-derives
    it.
    """

    #: Backend id used by the registry / --backend flag (e.g. "inprocess", "prefect").
    name: str = ""

    # --- template method (shared; do not override) -------------------------

    async def run_group(self, group: list[Node], run_node: RunNode) -> dict[str, NodeOutcome]:
        """Execute one planned group and return per-node outcomes.

        Solo group -> run the single node inline. Parallel group (2+ nodes
        sharing a parallel_group) -> run them concurrently via the backend's
        `_execute_parallel`, mapping any node that failed to produce an outcome
        to NodeOutcome(status="degraded"). This branch + mapping is identical
        for every backend, so it lives here; only `_execute_parallel` varies.
        """
        if len(group) == 1:
            n = group[0]
            return {n.name: await run_node(n.name)}
        self.get_logger().info(f"PARALLEL group: {[n.name for n in group]}")
        names = [n.name for n in group]
        outcomes = await self._execute_parallel(names, run_node)
        # Defensive: any name the backend didn't return -> degraded (never drop a node).
        return {name: outcomes.get(name, NodeOutcome(status="degraded")) for name in names}

    # --- abstract primitives (backend-specific) ----------------------------

    @abc.abstractmethod
    async def run_session(self, name: str, work: Callable[[], Awaitable[dict[str, NodeOutcome]]]) -> dict[str, NodeOutcome]:
        """Execute the whole pipeline `work` coroutine within this backend's context.

        `work` performs the DAG walk (calling back into run_group). A backend that
        needs an ambient execution context establishes it here: PrefectBackend
        runs `work` inside a `@flow` (so `.submit()` / `get_run_logger()` work);
        InProcessBackend just awaits `work()` directly. `name` labels the session
        (the Prefect flow name). Returns `work`'s result unchanged.
        """

    @abc.abstractmethod
    async def _execute_parallel(self, node_names: list[str], run_node: RunNode) -> dict[str, NodeOutcome]:
        """Run the named nodes CONCURRENTLY and return {name: outcome}.

        A node whose execution raised (rather than returning a NodeOutcome) may
        be omitted or mapped to a degraded outcome — run_group backfills any
        missing name to degraded, so either is safe. Must not raise for a single
        node's failure (a blocking failure surfaces as NodeBlocked out of
        run_node and MUST propagate to abort the run).
        """

    @abc.abstractmethod
    async def apply_concurrency_limit(self, tag: str, limit: int, info: Callable[[str], None], warn: Callable[[str], None]) -> None:
        """Best-effort: bound concurrent node execution to `limit` (on tag `tag`)."""

    @abc.abstractmethod
    def get_logger(self) -> Any:
        """A logger for engine + node lines (run-tagged if the backend supports it).

        Returns any object exposing `.info/.warning/.error` that accepts a single
        pre-formatted message string — loguru's `logger` (InProcessBackend, the
        house standard) or Prefect's `get_run_logger()` (PrefectBackend, so node
        lines land in the run UI). The engine passes only f-string messages, so
        both sinks behave identically."""

    @abc.abstractmethod
    def bootstrap(self) -> None:
        """One-time setup before a run (env, server, …). No-op if none needed."""

    @abc.abstractmethod
    def teardown(self) -> None:
        """One-time cleanup after a run. No-op if none needed."""
