"""Mock runner — a local, no-token stub used for demos and tests.

Spawns the packaged `_mock_agent.py` (a sibling of the package), which writes a
status sidecar and exits fast. No event stream, no model, no external tools —
completion is detected via the sidecar, like every runner.
"""

from __future__ import annotations

from pathlib import Path

from agent_flow.runners.base import AgentInvocation, AgentRunnerInfo, Event


class MockRunner:
    """Local stub runner: spawns the packaged _mock_agent.py; no event stream."""

    name = "mock"

    def __init__(self, stub: Path | None = None) -> None:
        # _mock_agent.py ships inside the package (top-level agent_flow module).
        self._stub = stub or (Path(__file__).resolve().parents[1] / "_mock_agent.py")

    def build_command(self, inv: AgentInvocation) -> list[str]:
        cmd = ["python3", str(self._stub), "--agent", inv.agent, "--prompt", inv.prompt]
        if inv.model:
            cmd += ["--model", inv.model]
        return cmd

    def parse_event(self, line: str) -> Event:
        return Event.none()  # mock finishes fast; completion is via sidecar

    def info(self, agent_dir: str | Path | None = None) -> AgentRunnerInfo:
        return AgentRunnerInfo(name=self.name, available=True, detail="local no-token stub; no model, no external tools")
