"""AgentExecutor — the seam that RUNS one agent invocation to an AgentResult.

This is the real execution seam. Its contract is minimal and runtime-neutral:

    run(inv: AgentInvocation) -> AgentResult

Given the complete, neutral `AgentInvocation` (the composed prompt, run_dir,
model, result_schema, shared blocks, on_event, ...) an executor produces an
`AgentResult` (control envelope + telemetry + validated typed object). HOW it
does so is the executor's business:

  - SubprocessExecutor spawns a CLI runtime (opencode / Claude Code / ...),
    supervises it by liveness, kills on stale, and reads a control SIDECAR the
    agent writes. It delegates the two wire details (build the argv, parse a
    stdout line into an Event) to an `AgentRunner`, which is therefore its
    PRIVATE strategy — not the public seam.

  - an in-process executor (e.g. PydanticAI) calls a Python function/agent
    directly: no subprocess, no supervision, no kill, no sidecar, no control
    preamble. It maps the call's typed return onto AgentResult.

Contrast with the two OTHER seams:
  - `AgentRunner` (runners/base.py) — the subprocess WIRE adapter
    (build_command + parse_event), owned by SubprocessExecutor. NOT this seam.
  - `FlowBackend` (backends/) — how a GROUP of nodes is executed (inprocess /
    prefect). Orthogonal: a backend runs many nodes; an executor runs one agent.

An ABC (not a Protocol) because concrete executors DO share logic — result
assembly and the status->exception policy can live in the base — mirroring the
FlowBackend ABC decision. The subprocess machinery (spawn/supervise/kill/
sidecar) lives with the concrete SubprocessExecutor in core/agent_runtime.py,
next to the helpers it needs.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field

from agent_flow.protocol import coerce_schema
from agent_flow.runners.invocation import AgentInvocation

# Canonical separator for a runtime-qualified agent label: "<runtime>:<agent>"
# (e.g. "opencode:my-agent", "inproc:some-agent", "mock:some-agent"). A colon is
# unambiguous — neither runtime names nor agent names use one, unlike "/" which
# can appear in path-like names. Defined ONCE here; render via qualified_agent().
RUNTIME_AGENT_SEP = ":"


def qualified_agent(runtime: str, agent: str) -> str:
    """Format a runtime-qualified agent label, e.g. "opencode:my-agent".

    Falls back to the bare agent when runtime is empty (a hand-written `run`
    node has no runtime), and to the bare runtime when agent is empty. The
    single source of truth for the label format (see RUNTIME_AGENT_SEP).
    """
    if not runtime:
        return agent
    if not agent:
        return runtime
    return f"{runtime}{RUNTIME_AGENT_SEP}{agent}"


class AgentTimeoutError(RuntimeError):
    """Raised when an agent goes STALE: no event and no sidecar for
    `idle_timeout_s`. This is a liveness timeout, not a wall-clock one — an
    actively-emitting agent is never killed regardless of elapsed time.
    Liveness supervision is SubprocessExecutor-only (nothing else is a process to
    supervise), so only it raises this."""


class AgentContentFailedError(RuntimeError):
    """Raised when an agent (or a mock_agent) reports a content failure via its
    control status.

    This is a genuine failure the agent diagnosed itself (e.g. could not parse
    the report, missing required input). Retrying the same prompt will not help,
    so the retry policy must NOT retry this class. Raised by the shared
    `AgentExecutor.check_content_status` — see that method.
    """


class AgentCrashError(RuntimeError):
    """Raised when an agent process exits non-zero with no error control signal.

    This represents a process-level crash (CLI error, OOM, rate-limit 429, …).
    It is transient and the retry policy SHOULD retry it. Subprocess-only (there
    is no process to crash for an in-process/mock executor).
    """


@dataclass(frozen=True)
class AgentResult:
    """Outcome of running one agent invocation — the executor seam's OUTPUT type.

    Runtime-neutral: a subprocess executor fills it from a control sidecar + event
    telemetry; an in-process executor fills it from a Python call's return. The
    fields mean the same thing regardless of how the agent ran.
    """

    agent: str
    exit_code: int | None
    duration_s: float
    # The executor's runtime label — the canonical name of HOW this agent ran:
    # a subprocess runtime ("opencode" / "claude"), "inproc", or "mock". Stamped
    # by each executor (its own `name`) via assemble_result. Empty only when a
    # result is built without going through an executor (e.g. bare unit tests).
    runtime: str = ""
    control: dict = field(default_factory=dict)  # status envelope (status/reason/rerun_required/result)
    # Telemetry (subprocess: harvested from the event stream; in-process: from the SDK usage).
    tokens: int = 0
    cost: float = 0.0
    events: int = 0
    # How the run terminated: "completed" | "sidecar" | "stale" (subprocess
    # supervision values; in-process/mock always use "completed").
    completion: str = "completed"
    # Result-schema validation outcome (only meaningful when a result_schema was
    # supplied). result_valid is True when no schema was given (nothing to fail).
    # result_obj is a pydantic model INSTANCE when a PydanticSchema was used, else
    # None (a dict schema / no schema produce no new object — the dict is already
    # in control["result"]). A gate reads these — the engine never auto-fails.
    result_valid: bool = True
    result_obj: object = None
    result_errors: tuple[str, ...] = ()


class AgentExecutor(abc.ABC):
    """Run one AgentInvocation to an AgentResult. Runtime-neutral seam."""

    name: str

    @abc.abstractmethod
    async def run(self, inv: AgentInvocation) -> AgentResult:
        """Execute the invocation and return an AgentResult (async — the engine
        awaits it).

        Implementations receive the FULL neutral invocation and must return a
        populated AgentResult (control envelope with a `status`, telemetry, and
        the validated `result_obj` when the invocation carried a result_schema).
        Whether that comes from a supervised subprocess + sidecar or an
        in-process call is the implementation's concern.

        The contract is a coroutine so async-native agent libraries (PydanticAI)
        are first-class: a subprocess executor `await`s its supervision loop; an
        in-process executor `await`s the consumer's impl if it is a coroutine (and
        offloads a blocking sync impl to a worker thread). A CPU-only executor may
        implement it as a plain `async def` that never awaits.

        Concrete subclasses MAY accept additional keyword-only arguments beyond
        `inv` for their own mechanism (e.g. `SubprocessExecutor` accepts an
        optional `control_file=` override). Such extra params are the subclass's
        private concern and must never be used through the `AgentExecutor` ABC
        type — callers that need them must hold the concrete type directly.
        """
        raise NotImplementedError

    # --- shared result-assembly tail ---------------------------------------
    # Concrete executors differ in HOW they obtain a control envelope (a
    # subprocess reads a sidecar; the mock calls a behaviour; ...), but the tail
    # is identical: validate the envelope's `result` against the invocation's
    # result_schema and build the AgentResult. Hoisted here so subclasses share
    # one implementation.
    @staticmethod
    def assemble_result(
        inv: AgentInvocation,
        control: dict,
        *,
        exit_code: int | None = 0,
        duration_s: float = 0.0,
        tokens: int = 0,
        cost: float = 0.0,
        events: int = 0,
        completion: str = "completed",
        runtime: str = "",
    ) -> "AgentResult":
        """Build an AgentResult from a control envelope, validating result_schema.

        The `result_obj` / `result_valid` / `result_errors` fields are populated
        ONLY via schema validation (identical to the subprocess path): with no
        schema, `result_valid` is True and `result_obj` is None. The raw payload
        is always in `control["result"]` regardless, so a gate can read it either
        way.
        """
        schema = coerce_schema(inv.result_schema)
        outcome = schema.validate(control.get("result", {})) if schema is not None else None
        return AgentResult(
            agent=inv.agent,
            exit_code=exit_code,
            duration_s=duration_s,
            runtime=runtime,
            control=control,
            tokens=tokens,
            cost=cost,
            events=events,
            completion=completion,
            result_valid=outcome.valid if outcome is not None else True,
            result_obj=outcome.obj if outcome is not None else None,
            result_errors=outcome.errors if outcome is not None else (),
        )

    @staticmethod
    def check_content_status(agent: str, control: dict) -> None:
        """Raise AgentContentFailedError unless the envelope's status is ok/verified.

        This is the DEFAULT fail-fast policy for a bad content status. It is the
        ONLY mechanism (short of a custom gate manually reading `ctx.result`)
        by which a bad status stops or degrades a node: `interpret()` catches the
        raised exception and maps it through the node's `criticality` (blocking ->
        NodeBlocked/halts; degrade -> recorded as "degraded"). None of the
        built-in gates (require_file, rerun_on_signal, rerun_on_named) inspect
        `status` themselves, so without this check a bad status would otherwise
        be silently treated as a success.

        Every executor that produces a content-derived envelope (subprocess sidecar
        or a mock_agent's return) must call this after assembling its AgentResult,
        so a bad status behaves IDENTICALLY regardless of execution model.
        SubprocessExecutor additionally raises AgentTimeoutError for its own
        stale/liveness case — that is subprocess-specific and not part of this
        shared check.
        """
        status = control.get("status")
        if status not in ("ok", "verified"):
            raise AgentContentFailedError(f"agent {agent!r} reported status={status!r}: {control.get('reason')}")
