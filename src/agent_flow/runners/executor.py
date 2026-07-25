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

from agent_flow.runners.base import AgentInvocation


class AgentExecutor(abc.ABC):
    """Run one AgentInvocation to an AgentResult. Runtime-neutral seam."""

    name: str

    @abc.abstractmethod
    def run(self, inv: AgentInvocation):  # -> AgentResult (untyped here to avoid a core import cycle)
        """Execute the invocation and return an AgentResult.

        Implementations receive the FULL neutral invocation and must return a
        populated AgentResult (control envelope with a `status`, telemetry, and
        the validated `result_obj` when the invocation carried a result_schema).
        Whether that comes from a supervised subprocess + sidecar or an
        in-process call is the implementation's concern.
        """
        raise NotImplementedError
