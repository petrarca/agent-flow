"""Concurrent flow runs must NOT share the run-context param store.

`arun_flow` is documented for async servers (a FastAPI handler), where two
requests run two flows CONCURRENTLY on one event loop in one process. The domain
param store must therefore be scoped to the run's task tree, not to the process.

The decisive case is the WRITE path: an `exports` hook publishes a value for
downstream nodes (`get_run_context().update(...)`). With a process-global store,
run A's export lands in run B's params — silent cross-run corruption, not just a
stale read.

These tests fail against a process-global singleton and pass once the store is a
task-scoped ContextVar.
"""

import anyio
import pytest

from agent_flow.flowdef import FlowDef, NodeDef
from agent_flow.registry import FlowRegistry


def _flow_with_export(seen: dict, tag: str):
    """A 2-node flow: `producer` EXPORTS a value, `consumer` templates it.

    `seen` records what the consumer actually received, keyed by run tag, so a
    cross-run leak is visible.
    """
    registry = FlowRegistry()

    @registry.mock_agent("producer")
    def _produce(inv, ctx):  # noqa: ARG001
        # Each run publishes a DIFFERENT value under the same param name.
        return {"status": "ok", "result": {"mode": f"mode-{tag}"}}

    @registry.mock_agent("waiter")
    async def _wait(inv, ctx):  # noqa: ARG001
        # Yield AFTER this run's export but BEFORE its consumer resolves
        # {mode}. That is the only gap where a shared store is observable: the
        # sibling run's export lands in between. Without this the test is
        # timing-dependent and passes even when the store IS shared.
        await anyio.sleep(0.05)
        return {"status": "ok"}

    @registry.export("publish_mode")
    def _publish(payload):
        # No result_schema on the node, so the payload is the whole control
        # envelope; the agent's own data sits under "result".
        return {"mode": ((payload or {}).get("result") or {}).get("mode", "")}

    @registry.mock_agent("consumer")
    def _consume(inv, ctx):  # noqa: ARG001
        seen[tag] = inv.inputs.get("MODE")
        return {"status": "ok"}

    flow = FlowDef(
        name=f"flow-{tag}",
        nodes=[
            NodeDef(name="producer", agent="producer", export_ref="publish_mode"),
            NodeDef(name="waiter", agent="waiter", depends_on=["producer"]),
            NodeDef(name="consumer", agent="consumer", depends_on=["waiter"], inputs={"MODE": "{mode}"}),
        ],
    )
    return flow, registry


async def _run(tag: str, seen: dict, tmp_path):
    from agent_flow import arun_flow

    flow, registry = _flow_with_export(seen, tag)
    await arun_flow(
        flow,
        registry=registry,
        run_dir=str(tmp_path / tag),
        mock_agents=True,
        # A distinct initial param per run, to catch read-side leakage too.
        product_key=f"product-{tag}",
        mode="unset",
    )


def test_concurrent_runs_do_not_share_exported_params(tmp_path):
    """THE regression: two flows on ONE event loop, each exporting a different
    value under the same param name. Each consumer must see its OWN run's value."""
    seen: dict = {}

    async def _both():
        async with anyio.create_task_group() as tg:
            tg.start_soon(_run, "a", seen, tmp_path)
            tg.start_soon(_run, "b", seen, tmp_path)

    anyio.run(_both)
    assert seen == {"a": "mode-a", "b": "mode-b"}, f"cross-run param leak: {seen}"


def test_sequential_runs_still_isolate(tmp_path):
    """The common (CLI) case must keep working: one run after another."""
    seen: dict = {}
    anyio.run(_run, "a", seen, tmp_path)
    anyio.run(_run, "b", seen, tmp_path)
    assert seen == {"a": "mode-a", "b": "mode-b"}


def test_store_is_not_shared_across_concurrent_task_trees():
    """Lower-level: the store object each run resolves must be distinct."""
    from agent_flow.run_context import get_run_context, init_run_context

    stores: dict = {}

    async def _one(tag: str):
        init_run_context({"who": tag})
        await anyio.sleep(0)  # yield, so the other run interleaves here
        stores[tag] = get_run_context().snapshot().get("who")

    async def _both():
        async with anyio.create_task_group() as tg:
            tg.start_soon(_one, "a")
            tg.start_soon(_one, "b")

    anyio.run(_both)
    assert stores == {"a": "a", "b": "b"}, f"the run-context store is shared across runs: {stores}"


@pytest.mark.parametrize("backend", ["inprocess", "prefect"])
def test_concurrent_runs_isolated_on_the_backend(tmp_path, backend):
    """Same guarantee when the nodes run through a backend's task group."""
    seen: dict = {}

    async def _run_with_backend(tag: str):
        from agent_flow import arun_flow

        flow, registry = _flow_with_export(seen, tag)
        await arun_flow(
            flow,
            registry=registry,
            run_dir=str(tmp_path / f"{backend}-{tag}"),
            mock_agents=True,
            run_config={"backend": backend},
            mode="unset",
        )

    async def _both():
        async with anyio.create_task_group() as tg:
            tg.start_soon(_run_with_backend, "a")
            tg.start_soon(_run_with_backend, "b")

    anyio.run(_both)
    assert seen == {"a": "mode-a", "b": "mode-b"}
