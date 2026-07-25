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

from agent_flow.runners.base import AgentInvocation


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
    control: dict = field(default_factory=dict)  # status envelope (status/reason/rerun_required/result)
    # Telemetry (subprocess: harvested from the event stream; in-process: from the SDK usage).
    tokens: int = 0
    cost: float = 0.0
    events: int = 0
    # How the run terminated: "completed" | "sidecar" | "stale" | "hard_cap".
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
    def run(self, inv: AgentInvocation) -> AgentResult:
        """Execute the invocation and return an AgentResult.

        Implementations receive the FULL neutral invocation and must return a
        populated AgentResult (control envelope with a `status`, telemetry, and
        the validated `result_obj` when the invocation carried a result_schema).
        Whether that comes from a supervised subprocess + sidecar or an
        in-process call is the implementation's concern.

        Concrete subclasses MAY accept additional keyword-only arguments beyond
        `inv` for their own mechanism (e.g. `SubprocessExecutor` accepts an
        optional `control_file=` override). Such extra params are the subclass's
        private concern and must never be used through the `AgentExecutor` ABC
        type — callers that need them must hold the concrete type directly.
        """
        raise NotImplementedError
