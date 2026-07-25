"""FlowRegistry — named gates, exports, and observing hooks for the engine.

Flow-control logic and result-routing are referenced by NAME on a node (data),
and the IMPLEMENTATIONS live here — so a node/definition stays serializable and
the code is centralized and reusable. Two kinds, kept distinct on purpose:

  - DECIDING gates: a `(GateContext) -> Directive` built by a named factory
    `(**gate_args) -> Gate`. A node references one via `gate_ref` + `gate_args`.
    The three built-ins (require_file / rerun_on_signal / rerun_on_named) are
    pre-seeded, so the common cases need NO user code.

  - OBSERVING hooks: `on(event)` callbacks the engine fires at lifecycle points
    (e.g. "after_node"). They observe/telemeter; they do NOT steer the flow (only
    a node's gate decides Continue/Restart/GoTo/Stop). Cross-cutting concerns.

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
# An observing hook: (node, outcome) -> None. Fired at a lifecycle event; the
# return value is ignored (observers never steer flow).
Hook = Callable[..., None]

# The known lifecycle events an observing hook may subscribe to.
_EVENTS = frozenset({"after_node"})


class FlowRegistry:
    """Per-flow registry of gate factories, export impls, and observing hooks."""

    def __init__(self, *, seed_builtins: bool = True) -> None:
        self._gates: dict[str, GateFactory] = {}
        self._exports: dict[str, ExportImpl] = {}
        self._hooks: dict[str, list[Hook]] = {e: [] for e in _EVENTS}
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

    def on(self, event: str) -> Callable[[Hook], Hook]:
        """Register an OBSERVING hook for a lifecycle `event` (decorator)."""
        if event not in _EVENTS:
            raise ValueError(f"unknown hook event {event!r} (known: {sorted(_EVENTS)})")

        def deco(fn: Hook) -> Hook:
            self._hooks[event].append(fn)
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

    def fire(self, event: str, /, *args: Any, **kwargs: Any) -> None:
        """Fire all observing hooks for `event`. Never raises (an observer must
        not break the run); a failing hook is swallowed by the caller's log."""
        for hook in self._hooks.get(event, ()):  # unknown event -> no hooks
            hook(*args, **kwargs)
