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
        # A gate is `(ctx, **config) -> Directive`. A node's gate_args are the
        # config, bound with functools.partial at resolve time; the engine then
        # calls the bound gate with just ctx. One shape for plain and configurable
        # gates alike.
        self._gates: dict[str, Callable[..., Any]] = {}
        self._exports: dict[str, ExportImpl] = {}
        self._runs: dict[str, Callable[..., Any]] = {}  # custom run impls (NodeDef.run_ref)
        self._schemas: dict[str, Any] = {}  # result-schema impls (NodeDef.result_schema by name)
        # event -> list of (node_scope, hook). node_scope is None (all nodes) or a
        # frozenset of node names; ignored for group events.
        self._hooks: dict[str, list[tuple[frozenset[str] | None, Hook]]] = {e: [] for e in _EVENTS}
        if seed_builtins:
            self._seed_builtin_gates()

    def _seed_builtin_gates(self) -> None:
        # The shipped gates are `(ctx, **config) -> Directive`; a node's gate_args
        # supply the config (e.g. rerun_on_signal(ctx, target=…)).
        self._gates["require_file"] = require_file
        self._gates["rerun_on_signal"] = rerun_on_signal
        self._gates["rerun_on_named"] = rerun_on_named

    # --- registration (decorator or direct) --------------------------------

    def gate(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a DECIDING gate `(ctx, **config) -> Directive` under `name`.

            # no per-node config — the common case:
            @registry.gate("stack_usable")
            def stack_usable(ctx):
                return Stop(...) if ... else Continue()

            # configured per node — extra keyword params are the config; the
            # node's gate_args supply them (bound at resolve time):
            @registry.gate("rerun_to")
            def rerun_to(ctx, *, target):
                return GoTo(target) if ... else Continue()

        A node references it via gate="<name>" (+ gate_args for the config). One
        function either way — no factory, no inner function. Re-registering a
        name overrides it.
        """

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._gates[name] = fn
            return fn

        return deco

    def export(self, name: str) -> Callable[[ExportImpl], ExportImpl]:
        """Register a named EXPORT impl `(payload) -> Mapping` (decorator)."""

        def deco(fn: ExportImpl) -> ExportImpl:
            self._exports[name] = fn
            return fn

        return deco

    def run(self, name: str) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
        """Register a custom RUN impl `(RunContext) -> dict` (decorator).

        A NodeDef references it via `run_ref` for a node whose work is NOT the
        standard 'run one agent' (which a NodeDef expresses via `agent`). The
        code lives here; the definition stays serializable data.
        """

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            self._runs[name] = fn
            return fn

        return deco

    def schema(self, name: str) -> Callable[[Any], Any]:
        """Register a named result-schema (decorator or direct call).

        A NodeDef references it via `result_schema="name"`. The value is any
        ResultSchema / JSON-schema dict / pydantic BaseModel subclass that
        run_agent accepts (coerce_schema handles the shapes).

            @reg.schema("TechStack")
            class TechStack(BaseModel): ...

            reg.schema("Ready")(ReadyModel)  # direct
        """

        def deco(obj: Any) -> Any:
            self._schemas[name] = obj
            return obj

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

    # --- resolution (used by the engine) -----------------------------------

    def build_gate(self, gate_ref: str | None, gate_args: dict[str, Any] | None) -> Gate | None:
        """Resolve a gate_ref (+args) to a Gate `(ctx) -> Directive`, or None.

        A registered gate is `(ctx, **config) -> Directive`. The node's gate_args
        are the config: bound now via functools.partial so the returned gate
        takes just ctx (the engine's calling convention). No gate_args -> the
        gate is used as-is.
        """
        if not gate_ref:
            return None
        try:
            fn = self._gates[gate_ref]
        except KeyError:
            raise ValueError(f"unknown gate {gate_ref!r} (registered: {sorted(self._gates)})") from None
        if not gate_args:
            return fn
        import functools

        return functools.partial(fn, **gate_args)

    def get_export(self, export_ref: str) -> ExportImpl:
        """Resolve a named export impl."""
        try:
            return self._exports[export_ref]
        except KeyError:
            raise ValueError(f"unknown export {export_ref!r} (registered: {sorted(self._exports)})") from None

    def get_run(self, run_ref: str) -> Callable[..., Any]:
        """Resolve a named custom run impl."""
        try:
            return self._runs[run_ref]
        except KeyError:
            raise ValueError(f"unknown run {run_ref!r} (registered: {sorted(self._runs)})") from None

    def get_schema(self, name: str) -> Any:
        """Resolve a named result-schema."""
        try:
            return self._schemas[name]
        except KeyError:
            raise ValueError(f"unknown result_schema {name!r} (registered: {sorted(self._schemas)})") from None

    def has_gate(self, name: str) -> bool:
        return name in self._gates

    def has_schema(self, name: str) -> bool:
        return name in self._schemas

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
