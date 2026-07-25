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
status, which the batteries node surfaces to the gate the same way as a sidecar.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from agent_flow.core.schema import coerce_schema
from agent_flow.runners.base import AgentInvocation
from agent_flow.runners.executor import AgentExecutor, AgentResult

# An in-process agent impl: given the neutral invocation, produce a result in one
# of the accepted return shapes (see adapt_result).
AgentImpl = Callable[[AgentInvocation], Any]


class InProcessExecutor(AgentExecutor):
    """AgentExecutor that runs an agent as a direct in-process call."""

    def __init__(self, impl: AgentImpl, *, name: str = "inprocess") -> None:
        self.impl = impl
        self.name = name

    def run(self, inv: AgentInvocation) -> AgentResult:
        # The impl gets the same composed prompt a subprocess agent would (minus
        # the subprocess-only control preamble). It is free to use or ignore it.
        start = time.monotonic()
        raw = self.impl(inv)
        duration = time.monotonic() - start
        result = adapt_result(raw, inv)
        # Stamp duration if the impl did not set one (an AgentResult it built may
        # already carry its own).
        if not result.duration_s:
            result.duration_s = duration
        return result


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

    schema = coerce_schema(inv.result_schema)

    # A pydantic model instance: the typed result object directly.
    dump = getattr(raw, "model_dump", None)
    if callable(dump):
        payload = dump()
        control = {"status": "ok", "agent": inv.agent, "result": payload}
        outcome = schema.validate(payload) if schema is not None else None
        return AgentResult(
            agent=inv.agent,
            exit_code=0,
            duration_s=0.0,
            control=control,
            completion="completed",
            result_valid=outcome.valid if outcome is not None else True,
            result_obj=outcome.obj if outcome is not None else raw,
            result_errors=outcome.errors if outcome is not None else (),
        )

    # A plain mapping payload.
    if isinstance(raw, Mapping):
        payload = dict(raw)
        control = {"status": "ok", "agent": inv.agent, "result": payload}
        outcome = schema.validate(payload) if schema is not None else None
        return AgentResult(
            agent=inv.agent,
            exit_code=0,
            duration_s=0.0,
            control=control,
            completion="completed",
            result_valid=outcome.valid if outcome is not None else True,
            result_obj=outcome.obj if outcome is not None else None,
            result_errors=outcome.errors if outcome is not None else (),
        )

    raise TypeError(f"in-process agent impl returned unsupported type {type(raw).__name__}; expected AgentResult, a pydantic model, or a Mapping")
