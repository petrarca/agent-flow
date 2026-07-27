"""InProcessBackend — the lightweight, Prefect-free default.

Runs the DAG IN THIS PROCESS: parallel groups via an anyio task group, the LLM
concurrency limit via an anyio.Semaphore, logging via the stdlib logger. No
separate engine, no server, no external dependency, fast startup. This is the
default backend; PrefectBackend is opt-in for the run UI / scheduling / scale.

Named for the mechanism, not a location: the contrast with PrefectBackend is
in-process execution vs Prefect's out-of-process engine (Prefect also runs on
the local machine, so "local" would be ambiguous). The concurrency limit here is
a per-process semaphore, not Prefect's server-side global limit — equivalent for
a single run, which is this backend's whole point.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import anyio
from loguru import logger as _LOGGER

from agent_flow.backends.base import FlowBackend, RunNode
from agent_flow.engine import NodeBlocked, NodeOutcome


def _first_of_type(eg: BaseExceptionGroup, exc_type: type[BaseException]) -> BaseException | None:
    """Return the first leaf exception of `exc_type` in a (possibly nested)
    BaseExceptionGroup, or None. Used to surface a NodeBlocked that anyio wrapped
    in a task-group ExceptionGroup so the engine's `except NodeBlocked` still fires.
    """
    match, _rest = eg.split(exc_type)
    if match is None:
        return None
    leaf: BaseException = match
    while isinstance(leaf, BaseExceptionGroup):
        leaf = leaf.exceptions[0]
    return leaf


class InProcessBackend(FlowBackend):
    """In-process backend: task-group fan-out + semaphore limit + stdlib logging."""

    name = "inprocess"

    def __init__(self) -> None:
        # None => no limit; set by apply_concurrency_limit. Guards node execution
        # so a parallel fan-out cannot exceed the model provider's rate limit.
        self._sema: anyio.Semaphore | None = None

    async def _guarded(self, run_node: RunNode, node_name: str, out: dict[str, NodeOutcome]) -> None:
        """Run one node into `out`, holding the concurrency semaphore if set.

        A NodeBlocked (a blocking node's failure) is left to PROPAGATE: it escapes
        the child task, cancels the sibling tasks, and aborts the run — the exact
        semantics the threadpool version enforced by re-raising. Any OTHER
        exception is a non-blocking node error, so it maps to a degraded outcome
        for that node and never crashes the group (run_group backfills missing).
        """
        try:
            if self._sema is None:
                out[node_name] = await run_node(node_name)
            else:
                async with self._sema:
                    out[node_name] = await run_node(node_name)
        except NodeBlocked:
            raise  # blocking failure -> propagate (cancels siblings, aborts run)
        except Exception:  # noqa: BLE001 - a non-blocking node error degrades, never crashes the group
            out[node_name] = NodeOutcome(status="degraded")

    async def run_session(self, name: str, work: Callable[[], Awaitable[dict[str, NodeOutcome]]]) -> dict[str, NodeOutcome]:
        # No ambient context needed — run the walk directly in this process.
        return await work()

    async def _execute_parallel(self, node_names: list[str], run_node: RunNode) -> dict[str, NodeOutcome]:
        """Run nodes concurrently in an anyio task group.

        A NodeBlocked escaping a child cancels the group and propagates to abort
        the run; anyio wraps it in a BaseExceptionGroup, so we unwrap and re-raise
        the NodeBlocked to preserve the caller's `except NodeBlocked` contract.
        Any other per-node error is already handled in `_guarded` (-> degraded).
        """
        out: dict[str, NodeOutcome] = {}
        try:
            async with anyio.create_task_group() as tg:
                for node_name in node_names:
                    tg.start_soon(self._guarded, run_node, node_name, out)
        except BaseExceptionGroup as eg:
            blocked = _first_of_type(eg, NodeBlocked)
            if blocked is not None:
                raise blocked from eg
            raise
        return out

    async def apply_concurrency_limit(self, tag: str, limit: int, info: Callable[[str], None], warn: Callable[[str], None]) -> None:
        """Bound concurrent node execution to `limit` via a process-local semaphore."""
        self._sema = anyio.Semaphore(limit)
        info(f"LLM concurrency limit set to {limit} (in-process semaphore)")

    def get_logger(self) -> Any:
        return _LOGGER  # loguru's logger (the house standard)

    def bootstrap(self) -> None:
        return None  # no setup — the whole point of the in-process backend

    def teardown(self) -> None:
        return None  # nothing to tear down
