"""PrefectBackend — the opt-in backend backed by Prefect 3.

Preserves the original execution behavior: each node runs as a Prefect `@task`,
parallel groups fan out via `.submit()` + `futures.wait()`, logs go to the
run-tagged `get_run_logger()`, and the LLM concurrency limit is a global,
server-side limit on the task tag. Choose it (`--backend prefect` /
AGENT_FLOW_BACKEND=prefect) when you want the Prefect UI, run history,
scheduling, or scale. The lightweight default is InProcessBackend.

Since the async-first migration the node closure is a coroutine, so nodes run as
ASYNC Prefect tasks inside an ASYNC `@flow` — Prefect 3 is async underneath, so
this is a more natural fit. `.submit()` stays synchronous (it only blocks while
submitting), and the blocking future gather is offloaded to a worker thread so it
never stalls the engine's event loop.

Prefect is imported lazily INSIDE the methods so that importing this module (or
`agent_flow.backends`) never pulls Prefect — the core primitives stay
Prefect-free, guarded by the import-isolation test.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

import anyio

from agent_flow.backends.base import FlowBackend, RunNode
from agent_flow.flow_types import NodeOutcome


class PrefectBackend(FlowBackend):
    """Prefect-3 backed backend: async @task/@flow, submit/wait, server-side limit."""

    name = "prefect"

    def __init__(self, llm_tag: str = "llm") -> None:
        # The concurrency tag applied to every node task (for a shared limit).
        self._llm_tag = llm_tag

    async def run_session(self, name: str, work: Callable[[], Awaitable[dict[str, NodeOutcome]]]) -> dict[str, NodeOutcome]:
        """Run the walk inside an async Prefect @flow so submit()/get_run_logger() work."""
        from prefect import flow

        # Decorator-FACTORY form — `flow(name=...)(work)` rather than
        # `flow(work, name=...)`: identical at runtime, but it is the shape
        # Prefect's own type stubs model (they have no overload for a function
        # plus keyword options in one call).
        return await flow(name=name)(work)()

    async def _execute_parallel(self, node_names: list[str], run_node: RunNode) -> dict[str, NodeOutcome]:
        """Fan out nodes as async Prefect tasks and gather; a non-completed task degrades.

        `run_node` is a coroutine function, so `task(run_node)` is an async task.
        `.submit()` is synchronous (submission only); the blocking `wait` + result
        gather is run in a worker thread via anyio.to_thread so the engine's event
        loop is never stalled while the task runner drains.
        """
        from prefect import task
        from prefect.futures import wait

        # Wrap the engine's backend-agnostic run_node as a tagged Prefect task so
        # it participates in the run UI + the tag concurrency limit. Decorator-
        # FACTORY form for the same reason as run_session above.
        node_task = task(tags=[self._llm_tag], name="node")(run_node)
        futures = {name: node_task.submit(name) for name in node_names}

        def _gather() -> dict[str, NodeOutcome]:
            wait(list(futures.values()))
            # `run_node` is a coroutine function, so Prefect's stubs type the
            # future's payload as Awaitable[NodeOutcome]; at RUNTIME Prefect has
            # already resolved an async task's result, so `.result()` hands back
            # the NodeOutcome itself (covered by the prefect-parametrized
            # integration tests). Hence the cast.
            return {
                name: (cast("NodeOutcome", fut.result()) if fut.state.is_completed() else NodeOutcome(status="degraded"))
                for name, fut in futures.items()
            }

        return await anyio.to_thread.run_sync(_gather)

    async def apply_concurrency_limit(self, tag: str, limit: int, info: Callable[[str], None], warn: Callable[[str], None]) -> None:
        """Best-effort GLOBAL concurrency limit on a task tag (idempotent)."""
        import httpx
        from prefect.client.orchestration import get_client
        from prefect.exceptions import PrefectException

        # Already inside the engine's event loop — await the client directly
        # (no nested anyio.run, which would fail with a running loop).
        try:
            async with get_client() as client:
                await client.create_concurrency_limit(tag=tag, concurrency_limit=limit)
            info(f"LLM concurrency limit set to {limit}")
        except (PrefectException, httpx.HTTPError, OSError) as exc:
            warn(f"concurrency limit setup skipped: {exc}")

    def get_logger(self) -> Any:
        """Prefect's run logger inside a flow, else loguru.

        Inside a flow/task, Prefect's `get_run_logger()` routes node lines into
        the run UI; both it and loguru accept a single pre-formatted message, and
        the engine only passes f-strings, so either works. run_group / build_flow
        call this outside a task context too, so degrade gracefully to loguru when
        there is no active run.
        """
        try:
            from prefect import get_run_logger

            return get_run_logger()
        except Exception:  # noqa: BLE001 - no active run context -> loguru (house default)
            from loguru import logger

            return logger

    def bootstrap(self) -> None:
        """Set Prefect env defaults for a robust, self-contained local run.

        Owns what was `_prefect_env.bootstrap()` — called only when the Prefect
        backend is actually selected, so in-process runs never touch Prefect's
        env or spin a temporary server. Prefect is an optional dependency (the
        `prefect` extra); this is the first place a --backend prefect run touches
        it, so a missing install fails here with a clear, actionable message.
        """
        from agent_flow.backends._prefect_env import bootstrap
        from agent_flow.utils import require_extra

        # bootstrap() BEFORE require_extra, which imports prefect: prefect reads
        # its settings once, at first import, so any PREFECT_* set afterwards is
        # ignored for the life of the process. Bootstrapping second left
        # logging-to-API enabled, and the handler then queued records for an API
        # that does not exist — surfacing at interpreter exit as "Error logging
        # to API / All connection attempts failed". bootstrap() only writes
        # os.environ and imports nothing, so it is safe before the guard.
        bootstrap()
        require_extra("prefect", "prefect", "the Prefect execution backend (--backend prefect)")

    def teardown(self) -> None:
        return None  # Prefect's temp server manages its own lifecycle
