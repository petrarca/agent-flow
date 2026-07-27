"""InProcessExecutor — run an agent as an in-process Python callable.

The counterpart to SubprocessExecutor. Where the subprocess executor spawns a
CLI runtime and reads a control sidecar, this one just CALLS a Python function
(e.g. a PydanticAI agent) and maps its return onto an AgentResult. No subprocess,
no liveness supervision, no kill, no sidecar, no control preamble — "data in
(the invocation), typed data out".

The impl is any callable `impl(inv: AgentInvocation) -> <return>` where the
return is adapted to an AgentResult by `adapt_result` (below). It receives the
FULL neutral invocation (composed prompt via `compose_prompt`, model, run_dir,
result_schema, ...) — identical to what a subprocess executor gets — so the same
node definition runs on either execution model unchanged.

Return shapes accepted (mirrors how `exports` accepts a dict OR a callable):
  - AgentResult          — full control: the impl sets status/telemetry itself.
  - pydantic BaseModel   — the typed result object; wrapped as status "ok",
                           result payload = model.model_dump(), result_obj = model.
  - Mapping (dict)       — a result payload; wrapped as status "ok", result_obj
                           validated from the invocation's result_schema if any.

Errors: an exception from the impl is NOT swallowed — it propagates, and the
engine's per-node criticality maps it (blocking -> Stop, degrade -> degraded),
exactly as a subprocess crash would. The impl signals a CONTENT failure (a
verdict, retry-won't-help) by returning an AgentResult / dict with a non-ok
status, which the agent-node surfaces to the gate the same way as a sidecar.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace as dc_replace
from inspect import iscoroutinefunction
from typing import Any

import anyio

from agent_flow.runners.base import AgentInvocation
from agent_flow.runners.executor import AgentExecutor, AgentResult

# An in-process agent impl: given the neutral invocation, produce a result in one
# of the accepted return shapes (see adapt_result).
AgentImpl = Callable[[AgentInvocation], Any]


class InProcessExecutor(AgentExecutor):
    """AgentExecutor that runs an agent as a direct in-process call."""

    def __init__(self, impl: AgentImpl, *, name: str = "inproc") -> None:
        if not callable(impl):
            raise TypeError(f"InProcessExecutor: impl must be callable, got {type(impl).__name__!r}")
        self.impl = impl
        self.name = name

    async def run(self, inv: AgentInvocation) -> AgentResult:
        # The impl gets the same composed prompt a subprocess agent would (minus
        # the subprocess-only control preamble). It is free to use or ignore it.
        #
        # Additive sync/async support (the point of the async-first migration):
        #   - an ASYNC impl (`async def`, e.g. `await pydantic_ai_agent.run(...)`)
        #     is awaited inline on the loop (no bridge);
        #   - a SYNC impl (plain `def`) may block (network, disk) -> run it in a
        #     worker thread via anyio.to_thread so it never stalls the event loop.
        start = anyio.current_time()
        if iscoroutinefunction(self.impl):
            raw = await self.impl(inv)
        else:
            raw = await anyio.to_thread.run_sync(self.impl, inv)
        duration = anyio.current_time() - start
        result = adapt_result(raw, inv)
        # Stamp duration if the impl did not set one (an AgentResult it returned
        # may already carry its own). AgentResult is frozen; use replace().
        if not result.duration_s:
            result = dc_replace(result, duration_s=duration)
        # Stamp the runtime label authoritatively (this executor knows how the
        # agent ran; an impl's own AgentResult does not get to override it).
        return dc_replace(result, runtime=self.name)


def adapt_result(raw: Any, inv: AgentInvocation) -> AgentResult:
    """Adapt an impl's return value to an AgentResult.

    Accepts an AgentResult as-is; wraps a pydantic BaseModel or a Mapping into a
    status-"ok" AgentResult, validating against the invocation's result_schema
    when one is present so `result_obj` / `result_valid` match the subprocess
    path. An impl that wants the fully-composed prompt (with the run-wide blocks)
    can call `compose_prompt(inv)` itself; otherwise it reads inv.prompt directly.
    """
    if isinstance(raw, AgentResult):
        return raw

    # A pydantic model instance (model_dump) or a plain Mapping: wrap into a
    # status-"ok" control envelope and hand it to the SHARED result-assembly tail
    # (AgentExecutor.assemble_result), so schema validation / result_obj /
    # result_valid / result_errors are identical to the subprocess & mock paths.
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        payload = dump()
    elif isinstance(raw, Mapping):
        payload = dict(raw)
    else:
        raise TypeError(f"in-process agent impl returned unsupported type {type(raw).__name__}; expected AgentResult, a pydantic model, or a Mapping")

    control = {"status": "ok", "agent": inv.agent, "result": payload}
    return AgentExecutor.assemble_result(inv, control, exit_code=0, duration_s=0.0, completion="completed")
