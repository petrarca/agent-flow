"""Execution backends — the swappable seam for RUNNING a planned DAG.

The engine owns the flow logic (plan, walk, jump-back, start_from/only,
run-context) and hands the backend a `run_node` closure; the backend decides how
groups EXECUTE (parallel fan-out), how to bound concurrency, which logger to use,
and its bootstrap/teardown lifecycle.

Two backends ship:
  - InProcessBackend (default, "inprocess") — runs the DAG in this process:
    threadpool + semaphore + stdlib logging. No Prefect, no server, fast startup.
  - PrefectBackend (opt-in, "prefect") — Prefect 3 (@task/@flow, submit/wait, run
    UI, server-side concurrency limit). Prefect is imported lazily so importing
    this package stays Prefect-free.

("inprocess" names the mechanism, not a location — Prefect can also run on the
local machine, so "local" would be ambiguous.)

The seam is an ABC (`FlowBackend`): a concrete template-method `run_group`
carries the shared solo-vs-parallel + degraded-mapping logic, and abstract
primitives (see `base.py`) supply what each backend does differently — how a
group executes, the concurrency limit, the logger, and the run lifecycle.
`get_backend(name)` resolves a fresh instance by name.
"""

from __future__ import annotations

from agent_flow.backends.base import FlowBackend, RunNode
from agent_flow.backends.inprocess import InProcessBackend

# Registry maps name -> a zero-arg (or default-arg) factory. PrefectBackend is
# imported lazily inside its factory so `import agent_flow.backends` never pulls
# Prefect (the core-Prefect-free guarantee). InProcessBackend is the default.
DEFAULT_BACKEND = "inprocess"


def _make_prefect(llm_tag: str = "llm") -> FlowBackend:
    from agent_flow.backends.prefect import PrefectBackend

    return PrefectBackend(llm_tag=llm_tag)


_BACKENDS: dict[str, object] = {
    "inprocess": lambda llm_tag="llm": InProcessBackend(),
    "prefect": _make_prefect,
}


def get_backend(name: str, *, llm_tag: str = "llm") -> FlowBackend:
    """Resolve a fresh backend instance by name (e.g. the --backend flag value).

    A fresh instance per call keeps per-run state (InProcessBackend's semaphore)
    isolated. `llm_tag` is threaded to backends that tag node execution for a
    concurrency limit (Prefect); InProcessBackend ignores it.
    """
    try:
        factory = _BACKENDS[name]
    except KeyError:
        raise ValueError(f"unknown backend {name!r} (available: {sorted(_BACKENDS)})") from None
    return factory(llm_tag)  # type: ignore[operator]


__all__ = [
    "FlowBackend",
    "RunNode",
    "InProcessBackend",
    "get_backend",
    "DEFAULT_BACKEND",
]
