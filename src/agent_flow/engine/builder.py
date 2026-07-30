"""`build_flow` — compile a Node graph into a runnable flow callable.

The Tier-3 entry point. Validates the declaration (unique names, known duration
names, node overrides), plans the groups, resolves the execution backend by
name, and returns a callable that walks the DAG and dispatches each group to
that backend.

The backend is resolved lazily inside the call so this module stays
backend-free at import time: the engine owns flow logic, a `FlowBackend` owns
execution.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import replace
from pathlib import Path
from typing import Any

from agent_flow.engine.dispatch import maybe_await
from agent_flow.engine.interpreter import _make_node_emitter, interpret
from agent_flow.engine.planner import plan_groups
from agent_flow.engine.walker import _resolve_entry, _walk
from agent_flow.flow_types import Node, NodeBlocked, NodeOutcome
from agent_flow.utils import resolve_duration


async def _fire_group_hook(registry: Any, event: str, group: list[Node], warn: Callable[[str], None], *extra: Any) -> None:
    """Fire an observing group hook (before_group/after_group). Not node-scoped.

    Like _fire_hook, each handler's result is awaited when awaitable so an
    `async def` group hook actually runs."""
    try:
        for result in registry.fire(event, group, *extra):
            await maybe_await(result)
    except Exception as exc:  # noqa: BLE001 - an observer must never break the run
        warn(f"{event} hook failed ({exc}) — ignored")


def _make_group_runner(backend_impl: Any, registry: Any, run_node: Any, logger: Any):
    """Wrap the backend's group execution with before_group/after_group hooks."""

    async def run_group(group: list[Node]) -> dict[str, NodeOutcome]:
        await _fire_group_hook(registry, "before_group", group, logger.warning)
        outcomes = await backend_impl.run_group(group, run_node)
        await _fire_group_hook(registry, "after_group", group, logger.warning, outcomes)
        return outcomes

    return run_group


def _check_durations(nodes: list[Node], durations: dict[str, int], node_overrides: dict[str, dict[str, Any]]) -> None:
    """Reject an unknown duration name at BUILD time, with the whole graph in hand.

    Same instinct as plan_groups rejecting cycles here: a typo'd duration must not
    survive until that node's turn comes, halfway through a paid run. Checks BOTH
    the flow-declared duration and a run-config per-node duration override, since
    either can carry the typo.
    """
    for node in nodes:
        override = node_overrides.get(node.name, {})
        name = override.get("duration") or node.duration
        if name:
            resolve_duration(node.name, name, durations)


def _check_node_overrides(nodes: list[Node], node_overrides: dict[str, dict[str, Any]]) -> None:
    """Reject a per-node override keyed by a name no node has.

    Before this, `--instruct typo=…` (and a `nodes:` typo) was silently dropped;
    a whole run could proceed with an override that never applied. Fail at build.
    """
    known = {n.name for n in nodes}
    unknown = set(node_overrides) - known
    if unknown:
        raise ValueError(f"per-node run config names unknown node(s) {sorted(unknown)} (flow has: {sorted(known)})")


def build_flow(
    nodes: list[Node],
    *,
    name: str = "agent-flow",
    llm_tag: str = "llm",
    llm_concurrency: int | None = None,
    on_event_factory: Callable[[str], Any] | None = None,
    on_node_event: Callable[[str, str, str | None, str], None] | None = None,
    run_instructions: str = "",
    run_additional_instructions: str = "",
    run_context: Iterable[str] | None = None,
    agent_dir: str = "",
    node_overrides: dict[str, dict[str, Any]] | None = None,
    durations: dict[str, int] | None = None,
    options: dict[str, Any] | None = None,
    backend: str = "inprocess",
    registry: Any = None,
):
    """Compile a Node graph into a runnable flow callable.

    Returns a callable `f(run_dir, start_from="", only="", **params) -> dict`
    that walks the DAG (`plan_groups`), fans out parallel groups concurrently,
    and runs each node via `interpret` (gate directives + criticality + bounded
    re-runs). `params` are threaded unchanged into every node's RunContext.

    The ENGINE owns all flow logic (plan/walk/jump-back/start_from/only/
    run-context); the selected `backend` supplies only execution mechanics
    (parallel fan-out, concurrency limit, logger, bootstrap/teardown). Backends
    are resolved lazily HERE so the engine module imports without pulling any
    backend (Prefect stays optional).

    Args:
        nodes: the pipeline DAG.
        name: flow name (used by the Prefect backend's @flow).
        llm_tag: concurrency tag applied to each node task (for a shared limit).
        llm_concurrency: if set, a concurrency limit on `llm_tag` (global on the
            Prefect backend; a per-process semaphore on the in-process backend).
        backend: execution backend name — "inprocess" (default; runs the DAG in
            this process via a threadpool, no Prefect) or "prefect" (opt-in;
            @task/@flow, run UI).
        on_event_factory: optional per-node event-printer factory (display label
            -> a per-event callback); the agent-node passes the NODE name as
            the label. Reaches each node via RunContext.on_event_factory. Kept
            here (build time) rather than in `params` because it is a callable
            (not serializable) and is engine plumbing, not a domain input.
        on_node_event: optional DAG-node lifecycle callback
            `(node_name, phase, status, agent) -> None`. Called with phase="start"
            (status=None) when a node begins and phase="finish" (status is the
            NodeOutcome status: "ok"/"degraded", or "failed" on a blocking error)
            when it ends — including on re-runs (a jumped-back node fires "start"
            again). `agent` is Node.agent (an informal display label; "" for
            hand-written nodes). Pure data (no rendering); a CLI turns it into a
            live view. Bound here at build time like on_event_factory (a
            non-serializable closure, engine plumbing not a domain input).
        run_instructions: optional run-wide brief injected into EVERY agent's
            prompt (e.g. a global directive from the CLI/start). Reaches each node
            via RunContext.run_instructions; an agent-node forwards it to
            run_agent.
        run_context: optional run-wide context SOURCES (file paths / globs)
            whose CONTENT is injected into every agent — rules/standards the
            agent must actually have. Read at run time (per node, so `{name}`
            templating works). Reaches each node via RunContext.run_context.
        agent_dir: optional DEFAULT directory where agent definitions live
            (opencode `--dir`); a node may override via agent_node(agent_dir=...).
            Reaches each node via RunContext.agent_dir. Templated at run time.
            Independent of run_dir (agents-here vs artifacts-there).
    """
    from agent_flow.backends import get_backend

    backend_impl = get_backend(backend, llm_tag=llm_tag)
    if registry is None:
        from agent_flow.registry import FlowRegistry

        registry = FlowRegistry()  # built-in gates only

    run_context_t = tuple(run_context or ())
    node_overrides_d = dict(node_overrides or {})
    durations_d = dict(durations or {})
    options_d = dict(options or {})
    _check_node_overrides(nodes, node_overrides_d)
    _check_durations(nodes, durations_d, node_overrides_d)
    planned = plan_groups(nodes)  # fail fast on cycles/unknown deps at build time
    by_name = {n.name: n for n in nodes}
    group_index = {key: i for i, (key, _) in enumerate(planned)}  # group key -> plan position
    node_group = {n.name: (n.parallel_group or n.name) for n in nodes}
    _emit = _make_node_emitter(on_node_event)

    def _make_run_node(wd: Path, params: dict, logger, pending: dict[str, str]) -> Callable[[str], Awaitable[NodeOutcome]]:
        """Build the backend-agnostic 'run ONE node' closure for this run.

        The returned closure is a COROUTINE function (the backend awaits it) —
        the same shape `backends.base.RunNode` names.

        Captures the resolved run_dir/params/logger. The backend calls it (inline
        or on N threads / Prefect tasks) but never decides what running a node
        MEANS — that is interpret(gate directives + criticality + bounded re-runs)
        plus start/finish emits and duration stamping, all here.

        `pending` is the run's {node_name: one-time-instruction} store: when the
        walker jumps back to a node it records the GoTo's instruction here, and
        this closure pops it (once) to hand the target node's re-run its one-time
        instruction. Empty for a normal forward run.
        """

        async def run_node(node_name: str) -> NodeOutcome:
            node = by_name[node_name]
            started = time.monotonic()
            # Pop this node's pending one-time instruction (from a prior cross-node
            # GoTo), if any — delivered to exactly this run, then gone.
            attempt_instruction = pending.pop(node_name, "")

            def _on_error(n: Node, exc: Exception) -> str:
                # Log only the first line (the real cause). The full detail —
                # command, sidecar parenthetical — travels on the raised
                # NodeBlocked and is printed once by the CLI, so we don't
                # duplicate the multi-line reason in the log.
                summary = str(exc).splitlines()[0] if str(exc) else exc.__class__.__name__
                if n.criticality == "blocking":
                    logger.error(f"BLOCKING node {n.name} failed: {summary}")
                    _emit(n.name, "finish", "failed", n.agent)
                    raise NodeBlocked(f"{n.name}: {exc}") from exc
                logger.warning(f"DEGRADE node {n.name} failed: {summary} — continuing")
                return "degraded"

            logger.info(f"node {node.name}: start (criticality={node.criticality})")
            _emit(node.name, "start", None, node.agent)
            # on_event_factory / run_instructions / run_context / agent_dir
            # are build-time values threaded into every node.
            outcome = await interpret(
                node,
                run_dir=wd,
                params=params,
                on_error=_on_error,
                log=logger.info,
                on_event_factory=on_event_factory,
                run_instructions=run_instructions,
                run_additional_instructions=run_additional_instructions,
                run_context=run_context_t,
                agent_dir=agent_dir,
                node_overrides=node_overrides_d,
                durations=durations_d,
                options=options_d,
                registry=registry,
                one_time_instruction=attempt_instruction,
            )
            # Stamp the node's wall-clock duration (timed here, where it runs).
            outcome = replace(outcome, duration_s=time.monotonic() - started)
            logger.info(f"node {node.name}: {outcome.status} ({outcome.duration_s:.1f}s)")
            _emit(node.name, "finish", outcome.status, node.agent)
            return outcome

        return run_node

    async def _pipeline(run_dir: str = "", start_from: str = "", only: str = "", **params: Any) -> dict:
        from agent_flow.run_context import init_run_context
        from agent_flow.utils import resolve_run_dir, resolve_template

        # Install the run-scoped domain-param store from the initial params.
        # Nodes read a snapshot (so upstream exports are visible) and export hooks
        # write to it. Same-process, run-scoped (see run_context SCOPE).
        init_run_context(params)
        # run_dir supports the same `{param}` templating as node inputs, but
        # STRICT: a path is never valid half-substituted, so a missing placeholder
        # is a hard error (not a dir literally named "{key}").
        try:
            resolved_run_dir = resolve_template(run_dir, params, strict=True)
        except KeyError as exc:
            raise ValueError(f"run_dir template references unknown param {exc}; available params: {sorted(params)}") from exc
        # Empty run_dir -> a fresh dir under <temp>/agent-flow/ (never cwd).
        wd = resolve_run_dir(resolved_run_dir, name=name)
        wd.mkdir(parents=True, exist_ok=True)

        async def _walk_session() -> dict[str, NodeOutcome]:
            # Runs INSIDE the backend's execution context (a Prefect @flow for the
            # prefect backend; directly for local) — so the logger, concurrency
            # limit, and node submission all bind to that context here.
            logger = backend_impl.get_logger()
            logger.info(f"run_dir: {wd}")
            if llm_concurrency is not None:
                await backend_impl.apply_concurrency_limit(llm_tag, llm_concurrency, logger.info, logger.warning)
            # Run-scoped {node: one-time instruction} store; the walker fills it on
            # a cross-node GoTo, run_node drains it into the target's re-run.
            pending_instructions: dict[str, str] = {}
            run_node = _make_run_node(wd, params, logger, pending_instructions)
            run_group = _make_group_runner(backend_impl, registry, run_node, logger)
            start_index, single_group = _resolve_entry(start_from, only, by_name, group_index, node_group, logger)
            results = await _walk(
                planned,
                run_group=run_group,
                group_index=group_index,
                node_group=node_group,
                by_name=by_name,
                logger=logger,
                start_index=start_index,
                single_group=single_group,
                pending_instructions=pending_instructions,
            )
            # Compact {node: status} summary — not the verbose NodeOutcome reprs.
            logger.info(f"{name} done: { {n: oc.status for n, oc in results.items()} }")
            return results

        backend_impl.bootstrap()
        try:
            return await backend_impl.run_session(name, _walk_session)
        finally:
            backend_impl.teardown()

    return _pipeline
