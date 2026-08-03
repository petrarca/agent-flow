"""The RUNNER contract — how the library talks to one subprocess runtime.

`AgentRunner` is the LOW-LEVEL seam, owned by `SubprocessExecutor` and not the
public one. A runtime implements exactly two things:

  build_command(inv) -> LaunchSpec   the argv (and how to read its streams)
  parse_event(line)  -> Event        one wire line as a neutral Event

A Protocol rather than an ABC because there is no shared implementation to
hoist: opencode and Claude Code have nothing in common but the shape.

The high-level seam is `AgentExecutor` (executor.py): "run one invocation,
produce a result". `SubprocessExecutor` is the implementation that owns a runner;
`InProcessExecutor` has no runner at all, because a Python call has no argv and
no event stream. The control sidecar is likewise SubprocessExecutor's private
mechanism — not part of this contract.

The vocabulary this contract is written in lives beside it:

  invocation.py  AgentInvocation — the neutral request, plus compose_prompt
  prompt.py      PromptParts and the default renderer
  events.py      Event — the neutral view of one thing an agent did
  spec.py        RunnerSpec / LaunchSpec / Check — how a runner declares itself
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from agent_flow.runners.events import Event
from agent_flow.runners.invocation import AgentInvocation
from agent_flow.runners.spec import LaunchSpec, RunnerSpec


@runtime_checkable
class RunnerBase(Protocol):
    """Common base for ALL runners regardless of transport.

    Carries the two things every runner must answer independently of how it is
    launched: its static `spec()` (identity + requirements) and `parse_event`
    (turn one unit of the runtime's event stream into a neutral Event). The event
    vocabulary is the SAME across transports — an opencode `session.idle` means
    the same thing whether it arrived on stdout (subprocess) or SSE (remote) — so
    `parse_event` lives here and is shared by both transport sub-protocols.

    THE VERDICT PROTOCOL. How the agent is TOLD to report its outcome is
    runtime-specific and belongs to the runner:

      - `build_verdict_preamble(agent, control_file, result_schema, rerun) -> str`
        (OPTIONAL): the completion-protocol instruction block prepended to the
        prompt. A sidecar-style runner returns the "write CONTROL_FILE" block; a
        structured-output runner returns a "return your final structured output"
        block. Pure/stateless — no I/O. When a runner does NOT implement it, the
        executor falls back to the shared `build_control_preamble` (sidecar).
        `rerun` is the node's re-run GRANT (protocol.RerunSpec) or None; a runner
        that supports the lever must describe it in the block it returns.

    HARVESTING the verdict is deliberately NOT a runner method — it is a
    POST-COMPLETION step that needs STATE the executor holds (the sidecar path,
    or the HTTP response + client + session id). The executor owns the state and
    the fetch. Only the runtime-specific INTERPRETATION of an already-fetched
    remote response (e.g. opencode's `info.structured`) becomes a stateless
    `parse_verdict(response) -> dict` on the remote runner — same `parse_*` family
    as `parse_event`. The subprocess sidecar is plain JSON needing no
    interpretation, so no `parse_verdict` there.

    OPTIONAL (a runner may implement them; callers use getattr/hasattr):
      - `build_verdict_preamble(...)`: see above.
      - `preflight_checks(agent_dir) -> list[Check]`: runtime pre-conditions.
      - `info(agent_dir=None) -> AgentRunnerInfo`: best-effort diagnostics.
    """

    def spec(self) -> RunnerSpec:
        """Return this runner's static identity + requirements."""
        ...

    def parse_event(self, raw: Any) -> Event:
        """Parse one unit of the runtime's event stream into a neutral Event.

        `raw` is a stdout line (str) for a subprocess runner or an already-decoded
        event dict for an http-sse runner — the runner knows its own shape. Typed
        `Any` (not `object`) precisely BECAUSE the shape is transport-specific:
        each sub-protocol pins it (`AgentRunner` takes a `str` line), and a
        narrower base would make every concrete runner an invalid override.
        """
        ...


@runtime_checkable
class AgentRunner(RunnerBase, Protocol):
    """Subprocess-transport runner: build an argv, parse stdout lines.

    REQUIRED (on top of RunnerBase): `build_command` (-> LaunchSpec). Everything
    else (supervision, kill, sidecar, DAG) is runner-agnostic and owned by
    SubprocessExecutor.

    OPTIONAL (a runner may implement them; callers use getattr/hasattr):
      - `preflight_checks(agent_dir) -> list[Check]`: runtime pre-conditions this
        runner needs (binary on PATH, not-nested, expected agent-dir layout).
        `preflight.check` asks the selected runner for these — so runtime
        specifics live in the runner, not the generic preflight module.
      - `info(agent_dir=None) -> AgentRunnerInfo`: a best-effort diagnostic
        self-description (version, resolved model, tools). Takes agent_dir because
        a runtime's config (model, tools) is resolved RELATIVE TO that dir — the
        same runner in a different agent_dir may see a different model/tool set,
        so this is effectively a per-node property. Must not raise.
    """

    name: str

    def build_command(self, inv: AgentInvocation) -> LaunchSpec:
        """Build the LaunchSpec (argv + diagnosis-safe display) for one invocation."""
        ...

    def parse_event(self, line: str) -> Event:
        """Parse one stdout line into an Event (liveness + tokens/cost)."""
        ...
