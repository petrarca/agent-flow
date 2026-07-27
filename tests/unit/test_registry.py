"""Unit tests for FlowRegistry + its wiring into the engine (interpret/build_flow)."""

import anyio
import pytest

from agent_flow.engine import Node, build_flow, interpret
from agent_flow.gates import Continue, Stop
from agent_flow.registry import FlowRegistry
from agent_flow.run_context import clear_run_context, init_run_context

# --- FlowRegistry in isolation ----------------------------------------------


def test_builtin_gates_seeded():
    r = FlowRegistry()
    assert {"require_file", "rerun_on_signal", "rerun_on_named"} <= set(r._gates)
    assert all(r.has_gate(g) for g in ("require_file", "rerun_on_signal", "rerun_on_named"))


def test_no_seed_when_disabled():
    r = FlowRegistry(seed_builtins=False)
    assert r._gates == {}


def test_build_gate_from_ref_and_args():
    r = FlowRegistry()
    gate = r.build_gate("rerun_on_signal", {"target": "analyst"})
    assert callable(gate)


def test_build_gate_none_ref_returns_none():
    assert FlowRegistry().build_gate(None, {}) is None


def test_unknown_gate_raises():
    with pytest.raises(ValueError, match="unknown gate 'nope'"):
        FlowRegistry().build_gate("nope", {})


def test_plain_gate_registered_and_used_directly():
    # A plain (ctx)->Directive gate needs NO inner function/factory.
    r = FlowRegistry()

    @r.gate("plain_stop")
    def plain_stop(ctx):
        return Stop(reason="halt")

    gate = r.build_gate("plain_stop", {})
    assert isinstance(gate(None), Stop)


def test_factory_gate_still_works_with_args():
    # A keyword-only-arg gate is a factory; built with gate_args.
    r = FlowRegistry()

    @r.gate("goto_target")
    def goto_target(ctx, *, target):
        return Continue()

    # gate_args are bound via partial; the result is callable with just ctx.
    bound = r.build_gate("goto_target", {"target": "a"})
    assert callable(bound) and isinstance(bound(None), Continue)


def test_configurable_gate_binds_args():
    r = FlowRegistry()

    @r.gate("always_stop")
    def always_stop(ctx, *, reason="x"):
        return Stop(reason=reason)

    gate = r.build_gate("always_stop", {"reason": "boom"})
    assert isinstance(gate(None), Stop) and gate(None).reason == "boom"


def test_export_registration_and_resolution():
    r = FlowRegistry()

    @r.export("pick")
    def _e(payload):
        return {"x": payload["x"]}

    assert r.get_export("pick")({"x": "1"}) == {"x": "1"}


def test_unknown_export_raises():
    with pytest.raises(ValueError, match="unknown export 'nope'"):
        FlowRegistry().get_export("nope")


def test_on_unknown_event_raises():
    with pytest.raises(ValueError, match="unknown hook event"):
        FlowRegistry().on("no_such_event")


def test_fire_observing_hooks():
    r = FlowRegistry()
    seen = []

    @r.on("after_node")
    def _h(node, outcome):
        seen.append((node, outcome))

    r.fire("after_node", "n", "ok")
    assert seen == [("n", "ok")]


# --- registry wired into interpret / build_flow -----------------------------


def _run_one(node, registry=None):
    init_run_context({})
    try:
        return anyio.run(
            lambda: interpret(node, run_dir=__import__("pathlib").Path("."), params={}, on_error=lambda n, e: "degraded", registry=registry)
        )
    finally:
        clear_run_context()


def test_interpret_resolves_gate_ref():
    # A node referencing a custom registered gate that stops the run.
    r = FlowRegistry()

    @r.gate("stopper")
    def _f(ctx):
        return Stop(reason="halt")

    node = Node(name="a", run=lambda ctx: {"ok": True}, gate_ref="stopper")
    from agent_flow.engine import NodeBlocked

    with pytest.raises(NodeBlocked):
        _run_one(node, r)


def test_interpret_callable_gate_backcompat():
    # A hand-supplied callable gate still works (no ref) — used directly.
    node = Node(name="a", run=lambda ctx: {}, gate=lambda ctx: Continue())
    out = _run_one(node)
    assert out.status == "ok"


def test_export_ref_applied_to_run_context():
    r = FlowRegistry()

    @r.export("pub")
    def _e(payload):
        return {"stack": payload.get("stack")}

    init_run_context({})
    try:
        node = Node(name="a", run=lambda ctx: {"stack": "python"}, export_ref="pub")
        anyio.run(lambda: interpret(node, run_dir=__import__("pathlib").Path("."), params={}, on_error=lambda n, e: "degraded", registry=r))
        from agent_flow.run_context import get_run_context

        assert get_run_context().get("stack") == "python"
    finally:
        clear_run_context()


@pytest.mark.anyio
async def test_after_node_hook_fires_via_build_flow():
    r = FlowRegistry()
    fired = []

    @r.on("after_node")
    def _h(node, outcome):
        fired.append((node.name, outcome.status))

    node = Node(name="a", run=lambda ctx: {"ok": True})
    result = await build_flow([node], name="t", registry=r)(run_dir="")
    assert result["a"].status == "ok"
    assert fired == [("a", "ok")]


@pytest.mark.anyio
async def test_async_hooks_are_awaited():
    """An `async def` hook must actually RUN — registry.fire returns each
    handler's result so the engine can await the awaitable ones. Regression: a
    fire() that discarded returns left the coroutine un-awaited (hook silently
    never ran, with a RuntimeWarning). Covers node-scoped and group hooks, and
    proves sync hooks still work alongside them."""
    r = FlowRegistry()
    fired: list[str] = []

    @r.on("before_node")
    def _sync_hook(node):  # noqa: ARG001 - signature fixed by the event
        fired.append("sync-before-node")

    @r.on("after_node")
    async def _async_node_hook(node, outcome):  # noqa: ARG001
        fired.append("async-after-node")

    @r.on("before_group")
    async def _async_group_hook(group):  # noqa: ARG001
        fired.append("async-before-group")

    node = Node(name="a", run=lambda ctx: {"ok": True})
    result = await build_flow([node], name="t", registry=r)(run_dir="")
    assert result["a"].status == "ok"
    assert sorted(fired) == ["async-after-node", "async-before-group", "sync-before-node"]


@pytest.mark.anyio
async def test_all_lifecycle_events_fire_in_order():
    r = FlowRegistry()
    ev = []
    r.on("before_group")(lambda group: ev.append(("bg", [n.name for n in group])))
    r.on("after_group")(lambda group, outs: ev.append(("ag", sorted(outs))))
    r.on("before_node")(lambda node: ev.append(("bn", node.name)))
    r.on("after_node")(lambda node, out: ev.append(("an", node.name)))
    nodes = [
        Node(name="a", run=lambda ctx: {}),
        Node(name="b", run=lambda ctx: {}, depends_on=["a"]),
    ]
    await build_flow(nodes, name="t", registry=r)(run_dir="")
    assert ev == [
        ("bg", ["a"]),
        ("bn", "a"),
        ("an", "a"),
        ("ag", ["a"]),
        ("bg", ["b"]),
        ("bn", "b"),
        ("an", "b"),
        ("ag", ["b"]),
    ]


@pytest.mark.anyio
async def test_node_scoped_hook_fires_only_for_target():
    r = FlowRegistry()
    hit = []
    r.on("after_node", node="b")(lambda node, out: hit.append(node.name))
    nodes = [Node(name="a", run=lambda ctx: {}), Node(name="b", run=lambda ctx: {}, depends_on=["a"])]
    await build_flow(nodes, name="t", registry=r)(run_dir="")
    assert hit == ["b"]  # only the scoped node, not 'a'


@pytest.mark.anyio
async def test_node_scoped_list():
    r = FlowRegistry()
    hit = []
    r.on("before_node", node=["a", "c"])(lambda node: hit.append(node.name))
    nodes = [
        Node(name="a", run=lambda ctx: {}),
        Node(name="b", run=lambda ctx: {}, depends_on=["a"]),
        Node(name="c", run=lambda ctx: {}, depends_on=["b"]),
    ]
    await build_flow(nodes, name="t", registry=r)(run_dir="")
    assert sorted(hit) == ["a", "c"]


@pytest.mark.anyio
async def test_on_error_hook_fires():
    r = FlowRegistry()
    errs = []
    r.on("on_error")(lambda node, exc: errs.append((node.name, str(exc))))

    def boom(ctx):
        raise RuntimeError("kaboom")

    nodes = [Node(name="a", run=boom, criticality="degrade")]
    await build_flow(nodes, name="t", registry=r)(run_dir="")
    assert errs and errs[0][0] == "a" and "kaboom" in errs[0][1]


def test_node_scope_rejected_for_group_event():
    with pytest.raises(ValueError, match="not node-scoped"):
        FlowRegistry().on("before_group", node="a")


@pytest.mark.anyio
async def test_default_registry_when_none():
    # build_flow with no registry seeds a default (built-in gates); a node using
    # a built-in gate ref resolves without the caller supplying a registry.
    node = Node(name="a", run=lambda ctx: {"ok": True}, gate_ref="rerun_on_signal", gate_args={"target": "a"})
    result = await build_flow([node], name="t")(run_dir="")
    assert result["a"].status == "ok"  # no sidecar -> rerun_on_signal returns Continue
