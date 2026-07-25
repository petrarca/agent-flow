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

from agent_flow.runners.base import AgentInvocation, AgentRunner, AgentRunnerInfo, Event
from agent_flow.runners.claude_code import ClaudeCodeRunner
from agent_flow.runners.mock import MockRunner
from agent_flow.runners.opencode import OpenCodeRunner

# Registry — string (from the spec) -> runner instance.
RUNNERS: dict[str, AgentRunner] = {
    "opencode": OpenCodeRunner(),
    "mock": MockRunner(),
    # "claude": ClaudeCodeRunner(),   # register when implemented
    # "codex":  CodexRunner(),
}


def get_runner(name: str) -> AgentRunner:
    """Resolve a runner by name (e.g. the "runtime" run param — "opencode" | "mock")."""
    try:
        return RUNNERS[name]
    except KeyError:
        raise ValueError(f"unknown runner {name!r} (available: {sorted(RUNNERS)})") from None


__all__ = [
    "AgentRunner",
    "AgentRunnerInfo",
    "AgentInvocation",
    "Event",
    "OpenCodeRunner",
    "MockRunner",
    "ClaudeCodeRunner",
    "RUNNERS",
    "get_runner",
]
