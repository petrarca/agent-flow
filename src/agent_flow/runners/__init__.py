"""Agent runners — the swappable backend for EXECUTING one agent.

This package is the runner SEAM. The neutral contract (the `AgentRunner`
Protocol + the `Event` / `AgentInvocation` / `AgentRunnerInfo` data types) lives
in `base.py`; each concrete runtime is a sibling module (`opencode.py`,
`mock.py`, `claude_code.py`). The registry + `get_runner` factory live here.

The seam is a `typing.Protocol` (structural), not an ABC: a runner matches by
SHAPE and does not inherit. There is no shared implementation to hoist —
build_command and parse_event are entirely runtime-specific — so a Protocol is
the right tool. (The execution-backend seam, which DOES share group-orchestration
logic, uses an ABC base instead.)
"""

from __future__ import annotations

from agent_flow.runners.base import DEFAULT_IDLE_TIMEOUT_S, AgentInvocation, AgentRunner, AgentRunnerInfo, Event, compose_prompt
from agent_flow.runners.claude_code import ClaudeCodeRunner
from agent_flow.runners.executor import AgentExecutor
from agent_flow.runners.inprocess import AgentImpl, InProcessExecutor
from agent_flow.runners.mock_exec import MockAgent, MockAgentContext, MockExecutor
from agent_flow.runners.opencode import OpenCodeRunner

# Registry — runtime string -> runner instance (the subprocess WIRE adapter).
# These are the REAL out-of-process runners. `mock` is deliberately NOT here: it
# is not a runtime but a substitution MODE (--mock-agents / MockExecutor).
RUNNERS: dict[str, AgentRunner] = {
    "opencode": OpenCodeRunner(),
    # "claude": ClaudeCodeRunner(),   # register when implemented
    # "codex":  CodexRunner(),
}


def get_runner(name: str) -> AgentRunner:
    """Resolve a runner (subprocess wire adapter) by name — e.g. "opencode"."""
    try:
        return RUNNERS[name]
    except KeyError:
        raise ValueError(f"unknown runner {name!r} (available: {sorted(RUNNERS)})") from None


def get_executor(name: str) -> AgentExecutor:
    """Resolve an AgentExecutor for a runtime name (the "runtime" run param).

    `runtime` names a REAL out-of-process runner, so this wraps the named runner
    in a SubprocessExecutor. Mock is NOT a runtime — it is the `--mock-agents`
    mode, routed to MockExecutor in node_builder, and never reaches here.
    """
    # Imported lazily: SubprocessExecutor lives in core.agent_runtime (next to the
    # subprocess machinery), which imports this package — avoid an import cycle.
    from agent_flow.core.agent_runtime import SubprocessExecutor

    try:
        runner = get_runner(name)
    except ValueError:
        raise ValueError(f"unknown runtime {name!r} (available: {sorted(RUNNERS)})") from None
    return SubprocessExecutor(runner)


__all__ = [
    "AgentRunner",
    "AgentRunnerInfo",
    "AgentInvocation",
    "AgentExecutor",
    "InProcessExecutor",
    "AgentImpl",
    "MockExecutor",
    "MockAgentContext",
    "MockAgent",
    "Event",
    "compose_prompt",
    "DEFAULT_IDLE_TIMEOUT_S",
    "OpenCodeRunner",
    "ClaudeCodeRunner",
    "RUNNERS",
    "get_runner",
    "get_executor",
]
