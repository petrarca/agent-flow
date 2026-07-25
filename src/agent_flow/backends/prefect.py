"""PrefectBackend — the opt-in backend backed by Prefect 3.

Preserves the original execution behavior: each node runs as a Prefect `@task`,
parallel groups fan out via `.submit()` + `futures.wait()`, logs go to the
run-tagged `get_run_logger()`, and the LLM concurrency limit is a global,
server-side limit on the task tag. Choose it (`--backend prefect` /
AGENT_FLOW_BACKEND=prefect) when you want the Prefect UI, run history,
scheduling, or scale. The lightweight default is LocalBackend.

Prefect is imported lazily INSIDE the methods so that importing this module (or
`agent_flow.backends`) never pulls Prefect — the core primitives stay
Prefect-free, guarded by the import-isolation test.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from agent_flow.backends.base import FlowBackend, RunNode
from agent_flow.engine import NodeOutcome


class PrefectBackend(FlowBackend):
    """Prefect-3 backed backend: @task/@flow, submit/wait, server-side limit."""

    name = "prefect"

    def __init__(self, llm_tag: str = "llm") -> None:
        # The concurrency tag applied to every node task (for a shared limit).
        self._llm_tag = llm_tag

    def run_session(self, name: str, work: Callable[[], dict[str, NodeOutcome]]) -> dict[str, NodeOutcome]:
        """Run the walk inside a Prefect @flow so submit()/get_run_logger() work."""
        from prefect import flow

        return flow(work, name=name)()

    def _execute_parallel(self, node_names: list[str], run_node: RunNode) -> dict[str, NodeOutcome]:
        """Fan out nodes as Prefect tasks and gather; a non-completed task degrades."""
        from prefect import task
        from prefect.futures import wait

        # Wrap the engine's backend-agnostic run_node as a tagged Prefect task so
        # it participates in the run UI + the tag concurrency limit.
        node_task = task(run_node, tags=[self._llm_tag], name="node")
        futures = {name: node_task.submit(name) for name in node_names}
        wait(list(futures.values()))
        out: dict[str, NodeOutcome] = {}
        for name, fut in futures.items():
            out[name] = fut.result() if fut.state.is_completed() else NodeOutcome(status="degraded")
        return out

    def apply_concurrency_limit(self, tag: str, limit: int, info: Callable[[str], None], warn: Callable[[str], None]) -> None:
        """Best-effort GLOBAL concurrency limit on a task tag (idempotent)."""
        import anyio
        import httpx
        from prefect.client.orchestration import get_client
        from prefect.exceptions import PrefectException

        async def _create() -> None:
            async with get_client() as client:
                await client.create_concurrency_limit(tag=tag, concurrency_limit=limit)

        # A pre-existing limit or a transient client error must not fail the run.
        try:
            anyio.run(_create)
            info(f"LLM concurrency limit set to {limit}")
        except (PrefectException, httpx.HTTPError, OSError) as exc:
            warn(f"concurrency limit setup skipped: {exc}")

    def get_logger(self) -> logging.Logger:
        """Prefect's run logger inside a flow, else the stdlib logger.

        run_group / build_flow call this outside a task context too, so degrade
        gracefully when there is no active run (mirrors batteries._node_logger).
        """
        try:
            from prefect import get_run_logger

            return get_run_logger()
        except Exception:  # noqa: BLE001 - no active run context -> stdlib
            return logging.getLogger("agent_flow")

    def bootstrap(self) -> None:
        """Set Prefect env defaults for a robust, self-contained local run.

        Owns what was `_prefect_env.bootstrap()` — called only when the Prefect
        backend is actually selected, so LocalBackend runs never touch Prefect's
        env or spin a temporary server. Prefect is an optional dependency (the
        `prefect` extra); this is the first place a --backend prefect run touches
        it, so a missing install fails here with a clear, actionable message.
        """
        from agent_flow.utils import require_extra

        require_extra("prefect", "prefect", "the Prefect execution backend (--backend prefect)")
        from agent_flow.backends._prefect_env import bootstrap

        bootstrap()

    def teardown(self) -> None:
        return None  # Prefect's temp server manages its own lifecycle
