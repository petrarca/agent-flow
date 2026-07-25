"""InProcessBackend — the lightweight, Prefect-free default.

Runs the DAG IN THIS PROCESS: parallel groups via a ThreadPoolExecutor, the LLM
concurrency limit via a threading.Semaphore, logging via the stdlib logger. No
separate engine, no server, no external dependency, fast startup. This is the
default backend; PrefectBackend is opt-in for the run UI / scheduling / scale.

Named for the mechanism, not a location: the contrast with PrefectBackend is
in-process execution vs Prefect's out-of-process engine (Prefect also runs on
the local machine, so "local" would be ambiguous). The concurrency limit here is
a per-process semaphore, not Prefect's server-side global limit — equivalent for
a single run, which is this backend's whole point.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from agent_flow.backends.base import FlowBackend, RunNode
from agent_flow.engine import NodeBlocked, NodeOutcome

_LOGGER = logging.getLogger("agent_flow")


class InProcessBackend(FlowBackend):
    """In-process backend: threadpool fan-out + semaphore limit + stdlib logging."""

    name = "inprocess"

    def __init__(self) -> None:
        # None => no limit; set by apply_concurrency_limit. Guards node execution
        # so a parallel fan-out cannot exceed the model provider's rate limit.
        self._sema: threading.Semaphore | None = None

    def _guarded(self, run_node: RunNode, node_name: str) -> NodeOutcome:
        """Run one node, holding the concurrency semaphore if a limit is set."""
        if self._sema is None:
            return run_node(node_name)
        with self._sema:
            return run_node(node_name)

    def run_session(self, name: str, work: Callable[[], dict[str, NodeOutcome]]) -> dict[str, NodeOutcome]:
        # No ambient context needed — run the walk directly in this process.
        return work()

    def _execute_parallel(self, node_names: list[str], run_node: RunNode) -> dict[str, NodeOutcome]:
        """Run nodes concurrently on a threadpool.

        A NodeBlocked (a blocking node's failure) must abort the whole run, so it
        is re-raised out of here. Any other unexpected exception maps to a
        degraded outcome for that node (run_group also backfills missing names).
        """
        out: dict[str, NodeOutcome] = {}
        with ThreadPoolExecutor(max_workers=len(node_names)) as pool:
            futures = {pool.submit(self._guarded, run_node, name): name for name in node_names}
            for fut, name in futures.items():
                try:
                    out[name] = fut.result()
                except NodeBlocked:
                    raise  # blocking failure -> propagate, aborts the run
                except Exception:  # noqa: BLE001 - a non-blocking node error degrades, never crashes the group
                    out[name] = NodeOutcome(status="degraded")
        return out

    def apply_concurrency_limit(self, tag: str, limit: int, info: Callable[[str], None], warn: Callable[[str], None]) -> None:
        """Bound concurrent node execution to `limit` via a process-local semaphore."""
        self._sema = threading.Semaphore(limit)
        info(f"LLM concurrency limit set to {limit} (in-process semaphore)")

    def get_logger(self) -> logging.Logger:
        return _LOGGER

    def bootstrap(self) -> None:
        return None  # no setup — the whole point of the in-process backend

    def teardown(self) -> None:
        return None  # nothing to tear down
