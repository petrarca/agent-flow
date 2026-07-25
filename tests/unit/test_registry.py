"""Unit tests for FlowRegistry + its wiring into the engine (interpret/build_flow)."""

import pytest

from agent_flow.engine import Node, build_flow, interpret
from agent_flow.gates import Continue, Stop
from agent_flow.registry import FlowRegistry
from agent_flow.run_context import clear_run_context, init_run_context

# --- FlowRegistry in isolation ----------------------------------------------


def test_builtin_gates_seeded():
    r = FlowRegistry()
    assert {"require_file", "rerun_on_signal", "rerun_on_named"} <= set(r._gates)


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


def test_custom_gate_decorator():
    r = FlowRegistry()

    @r.gate("always_stop")
    def _factory(reason="x"):
        def gate(ctx):
            return Stop(reason=reason)

        return gate

    gate = r.build_gate("always_stop", {"reason": "boom"})
    assert isinstance(gate(None), Stop)


def test_register_gate_callable_backcompat():
    r = FlowRegistry()
    ref = r.register_gate_callable(lambda ctx: Continue())
    assert callable(r.build_gate(ref, {}))


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
        return interpret(node, run_dir=__import__("pathlib").Path("."), params={}, on_error=lambda n, e: "degraded", registry=registry)
    finally:
        clear_run_context()


def test_interpret_resolves_gate_ref():
    # A node referencing a custom registered gate that stops the run.
    r = FlowRegistry()

    @r.gate("stopper")
    def _f():
        def g(ctx):
            return Stop(reason="halt")

        return g

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
        interpret(node, run_dir=__import__("pathlib").Path("."), params={}, on_error=lambda n, e: "degraded", registry=r)
        from agent_flow.run_context import get_run_context

        assert get_run_context().get("stack") == "python"
    finally:
        clear_run_context()


def test_after_node_hook_fires_via_build_flow():
    r = FlowRegistry()
    fired = []

    @r.on("after_node")
    def _h(node, outcome):
        fired.append((node.name, outcome.status))

    node = Node(name="a", run=lambda ctx: {"ok": True})
    result = build_flow([node], name="t", registry=r)(run_dir="")
    assert result["a"].status == "ok"
    assert fired == [("a", "ok")]


def test_default_registry_when_none():
    # build_flow with no registry seeds a default (built-in gates); a node using
    # a built-in gate ref resolves without the caller supplying a registry.
    node = Node(name="a", run=lambda ctx: {"ok": True}, gate_ref="rerun_on_signal", gate_args={"target": "a"})
    result = build_flow([node], name="t")(run_dir="")
    assert result["a"].status == "ok"  # no sidecar -> rerun_on_signal returns Continue
