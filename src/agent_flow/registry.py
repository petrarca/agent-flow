"""FlowRegistry — named gates, exports, and observing hooks for the engine.

Flow-control logic and result-routing are referenced by NAME on a node (data),
and the IMPLEMENTATIONS live here — so a node/definition stays serializable and
the code is centralized and reusable. Two kinds, kept distinct on purpose:

  - DECIDING gates: a `(GateContext) -> Directive` built by a named factory
    `(**gate_args) -> Gate`. A node references one via `gate_ref` + `gate_args`.
    The three built-ins (require_file / rerun_on_signal / rerun_on_named) are
    pre-seeded, so the common cases need NO user code.

  - OBSERVING hooks: `on(event)` callbacks the engine fires at lifecycle points
    (before_node / after_node / on_error, and before_group / after_group). The
    per-node events may be SCOPED to specific node names via `on(event, node=…)`.
    They observe/telemeter; they do NOT steer the flow (only a node's gate decides
    Continue/Restart/GoTo/Stop). Cross-cutting concerns.

Plus EXPORT impls: a named `(payload) -> Mapping[str, Any]` a node references via
`export_ref` (the declarative `{param: field}` form stays inline data on the
node and needs no registry).

Scope is per-flow: build a FlowRegistry, register your impls, and pass it to
`build_flow(..., registry=...)`. `build_flow` seeds a default (built-in gates
only) when none is given. No global state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from agent_flow.gates import Gate, require_file, rerun_on_named, rerun_on_signal

# A gate FACTORY: named, takes data kwargs, returns a Gate closure.
GateFactory = Callable[..., Gate]
# An export impl: maps a node's result payload to params for downstream nodes.
ExportImpl = Callable[[Any], Mapping[str, Any]]
# An observing hook: fired at a lifecycle event; the return value is IGNORED
# (observers never steer flow — only a node's gate decides Continue/GoTo/Stop).
Hook = Callable[..., None]

# The known lifecycle events an observing hook may subscribe to, with their args:
#   before_node(node)            — before each run attempt (fires on re-run cycles)
#   after_node(node, outcome)    — after the node settles
#   on_error(node, exc)          — the node's run raised (before criticality mapping)
#   before_group(group)          — before a (parallel or solo) group executes
#   after_group(group, outcomes) — after the group's nodes settle
# Per-node events (before_node/after_node/on_error) may be SCOPED to specific
# node names; group events are not node-scoped.
_NODE_EVENTS = frozenset({"before_node", "after_node", "on_error"})
_GROUP_EVENTS = frozenset({"before_group", "after_group"})
_EVENTS = _NODE_EVENTS | _GROUP_EVENTS


class FlowRegistry:
    """Per-flow registry of gate factories, export impls, and observing hooks."""

    def __init__(self, *, seed_builtins: bool = True) -> None:
        self._gates: dict[str, GateFactory] = {}
        self._exports: dict[str, ExportImpl] = {}
        # event -> list of (node_scope, hook). node_scope is None (all nodes) or a
        # frozenset of node names; ignored for group events.
        self._hooks: dict[str, list[tuple[frozenset[str] | None, Hook]]] = {e: [] for e in _EVENTS}
        if seed_builtins:
            self._seed_builtin_gates()

    def _seed_builtin_gates(self) -> None:
        # The shipped gate factories are already data-parameterized (they take
        # kwargs and return a Gate), so they register as-is; gate_args on a node
        # become these factories' kwargs.
        self._gates["require_file"] = require_file
        self._gates["rerun_on_signal"] = rerun_on_signal
        self._gates["rerun_on_named"] = rerun_on_named

    # --- registration (decorator or direct) --------------------------------

    def gate(self, name: str) -> Callable[[GateFactory], GateFactory]:
        """Register a DECIDING gate factory under `name` (decorator).

            @registry.gate("readiness_ok")
            def _factory(**kwargs) -> Gate:
                def gate(ctx): return Stop(...) if ... else Continue()
                return gate

        A node references it via gate_ref="readiness_ok" (+ gate_args passed to
        the factory). Re-registering a name overrides it.
        """

        def deco(factory: GateFactory) -> GateFactory:
            self._gates[name] = factory
            return factory

        return deco

    def export(self, name: str) -> Callable[[ExportImpl], ExportImpl]:
        """Register a named EXPORT impl `(payload) -> Mapping` (decorator)."""

        def deco(fn: ExportImpl) -> ExportImpl:
            self._exports[name] = fn
            return fn

        return deco

    def on(self, event: str, *, node: str | list[str] | tuple[str, ...] | None = None) -> Callable[[Hook], Hook]:
        """Register an OBSERVING hook for a lifecycle `event` (decorator).

        `node` scopes a per-node event (before_node/after_node/on_error) to one
        name or a list of names; None (default) means all nodes. `node` is not
        allowed for group events (before_group/after_group), which are not
        node-scoped. Observers never steer flow — a hook's return is ignored.
        """
        if event not in _EVENTS:
            raise ValueError(f"unknown hook event {event!r} (known: {sorted(_EVENTS)})")
        if node is not None and event in _GROUP_EVENTS:
            raise ValueError(f"event {event!r} is not node-scoped; drop node=")
        scope = None if node is None else frozenset((node,) if isinstance(node, str) else node)

        def deco(fn: Hook) -> Hook:
            self._hooks[event].append((scope, fn))
            return fn

        return deco

    def register_gate_callable(self, gate: Gate) -> str:
        """Auto-register an already-built Gate CALLABLE under a generated name.

        Back-compat path: agent_node(gate=<callable>) / a hand-written gate is
        stored as a zero-arg factory so it resolves through the same machinery as
        a named gate. Returns the generated ref.
        """
        ref = f"_callable_{id(gate):x}"
        self._gates[ref] = lambda **_: gate
        return ref

    # --- resolution (used by the engine) -----------------------------------

    def build_gate(self, gate_ref: str | None, gate_args: dict[str, Any] | None) -> Gate | None:
        """Resolve a gate_ref (+args) to a Gate, or None when no gate is set."""
        if not gate_ref:
            return None
        try:
            factory = self._gates[gate_ref]
        except KeyError:
            raise ValueError(f"unknown gate {gate_ref!r} (registered: {sorted(self._gates)})") from None
        return factory(**(gate_args or {}))

    def get_export(self, export_ref: str) -> ExportImpl:
        """Resolve a named export impl."""
        try:
            return self._exports[export_ref]
        except KeyError:
            raise ValueError(f"unknown export {export_ref!r} (registered: {sorted(self._exports)})") from None

    def fire(self, event: str, /, *args: Any, _node_name: str | None = None, **kwargs: Any) -> None:
        """Fire the observing hooks registered for `event`.

        For per-node events, pass `_node_name` so scoped hooks (registered with
        `node=`) match only their target; a None scope fires for every node.
        Group events ignore `_node_name`. The return of each hook is discarded.
        (The engine wraps this so a failing observer never breaks the run.)
        """
        for scope, hook in self._hooks.get(event, ()):  # unknown event -> no hooks
            if scope is not None and _node_name is not None and _node_name not in scope:
                continue
            hook(*args, **kwargs)
