"""Agent runners — the swappable backend for EXECUTING one agent.

`run_agent` (agent_runtime.py) is runner-agnostic: it spawns a subprocess,
supervises it by liveness, kills on stale, reads the status sidecar, and
harvests telemetry. What DIFFERS between opencode / Claude Code / Codex is only:

  1. how you build the command to run one agent, and
  2. how you parse the runner's stdout stream into liveness/telemetry events.

An `AgentRunner` owns exactly those two things. Everything else (supervision,
kill, sidecar, the DAG, re-runs, injection) is written once and runner-agnostic.

The status SIDECAR is deliberately NOT part of the runner: it is written by the
*agent* (via its Write tool) and read by the orchestrator, so it is identical
across every runner. That keeps the reliability contract uniform.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

DEFAULT_MODEL = "azure-claude/Claude Sonnet 4.6"


@dataclass(frozen=True)
class AgentInvocation:
    """Everything a runner needs to launch ONE agent.

    Carries the agent NAME and its resolved INSTRUCTIONS/PROMPT separately, so a
    runner can materialise agent identity its own way: opencode uses the named
    agent (`--agent <name>`) and passes the work-order as the prompt; a runner
    without named agents (Claude Code, Codex) can instead inject the agent's
    instructions as a system prompt and pass only the work-order. `prompt` is the
    per-run work order (PRODUCT_KEY, REPORT, CONTROL_FILE, ...); `instructions`
    is the agent's resolved standing context (may be empty for opencode, whose
    identity lives in its .md).
    """

    agent: str  # logical agent name (opencode --agent)
    prompt: str  # per-run work order
    model: str | None = None
    instructions: str = ""  # resolved standing instructions (for runners without named agents)
    agent_dir: str = ""  # absolute dir where agent DEFINITIONS live (opencode: --dir); "" = runtime default


@dataclass(frozen=True)
class Event:
    """One parsed unit of runner stdout — a liveness heartbeat + optional telemetry.

    The engine only needs tokens/cost/is_terminal/is_event for supervision. We do
    NOT parse the runner's event into a summary — that would couple us to each
    runner's (versioned) schema. Instead `raw` carries the ORIGINAL stdout line
    so a display callback can render it verbatim (e.g. rich's JSON pretty-print).
    """

    tokens: int = 0
    cost: float = 0.0
    is_terminal: bool = False
    is_event: bool = True  # False for lines that are not real events (ignored for liveness)
    raw: str = ""  # the original stdout line, for optional live display

    @staticmethod
    def none() -> Event:
        return Event(is_event=False)


@runtime_checkable
class AgentRunner(Protocol):
    """Strategy for one agent-execution backend. Owns command + event parsing only."""

    name: str

    def build_command(self, inv: AgentInvocation) -> list[str]:
        """The argv to spawn this runner for one agent invocation."""
        ...

    def parse_event(self, line: str) -> Event:
        """Parse one stdout line into an Event (liveness + tokens/cost)."""
        ...


# opencode


class OpenCodeRunner:
    """opencode: named agents via --agent; NDJSON event stream via --format json."""

    name = "opencode"

    def build_command(self, inv: AgentInvocation) -> list[str]:
        # opencode identity lives in the agent .md; we pass --agent + work order.
        # --auto auto-approves permissions not explicitly denied: a headless,
        # orchestrated runner is unattended, so writes into a fresh workspace
        # (which opencode otherwise flags as an "external directory" and
        # auto-REJECTS) must be allowed. The agent .md still explicitly denies
        # bash/webfetch, so --auto only greenlights the tools the agent permits.
        cmd = ["opencode", "run", "--agent", inv.agent, "--model", inv.model or DEFAULT_MODEL, "--format", "json", "--auto"]
        # --dir points opencode at the project where .opencode/agent lives, so
        # agents resolve regardless of the process cwd (opencode chdir's into it).
        if inv.agent_dir:
            cmd += ["--dir", inv.agent_dir]
        cmd.append(inv.prompt)
        return cmd

    def parse_event(self, line: str) -> Event:
        stripped = line.strip()
        if not stripped.startswith("{"):
            return Event.none()
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return Event.none()
        # Telemetry lives on step-finish parts; everything else is a plain
        # heartbeat. We do NOT interpret the event beyond that — `raw` carries
        # the full line for optional display.
        part = ev.get("part") or {}
        if part.get("type") in ("step-finish", "step_finish"):
            tokens = int((part.get("tokens") or {}).get("total") or 0)
            cost = float(part.get("cost") or 0.0)
            return Event(tokens=tokens, cost=cost, is_terminal=part.get("reason") == "stop", raw=stripped)
        return Event(raw=stripped)  # a real event (heartbeat), no telemetry


# mock (no LLM, no tokens) — used for token-free demos/tests


class MockRunner:
    """Local stub runner: spawns the packaged _mock_agent.py; no event stream."""

    name = "mock"

    def __init__(self, stub: Path | None = None) -> None:
        # _mock_agent.py ships inside the package (sibling of this module).
        self._stub = stub or (Path(__file__).resolve().parent / "_mock_agent.py")

    def build_command(self, inv: AgentInvocation) -> list[str]:
        return ["python3", str(self._stub), "--agent", inv.agent, "--prompt", inv.prompt, "--model", inv.model or DEFAULT_MODEL]

    def parse_event(self, line: str) -> Event:
        return Event.none()  # mock finishes fast; completion is via sidecar


# Stubs for future runners (documented, not implemented)


class ClaudeCodeRunner:
    """STUB with the REAL Claude Code CLI surface (verified) as a template.

    Claude Code headless mode:
      - one-shot:            claude -p "<prompt>"
      - streaming events:    --output-format stream-json  (newline-delimited JSON;
                             final event carries token usage + USD cost)
      - named agent:         --agent <name>   (Claude Code DOES support this)
      - model:               --model <name>   (+ optional --fallback-model)
      - inject instructions: --append-system-prompt "<inv.instructions>"

    Unlike opencode (whose identity lives in the agent .md), Claude Code takes
    the agent's standing instructions via --append-system-prompt — which is why
    AgentInvocation carries `instructions` separately from the work-order prompt.

    parse_event would decode Claude's stream-json event shape into an Event
    (tokens/cost from the final usage event; is_terminal on the finish event).
    Left unimplemented until we actually run Claude Code.
    """

    name = "claude"

    def build_command(self, inv: AgentInvocation) -> list[str]:  # pragma: no cover
        cmd = ["claude", "-p", inv.prompt, "--output-format", "stream-json", "--agent", inv.agent]
        if inv.model:
            cmd += ["--model", inv.model]
        if inv.instructions:
            cmd += ["--append-system-prompt", inv.instructions]
        return cmd

    def parse_event(self, line: str) -> Event:  # pragma: no cover
        raise NotImplementedError("decode Claude Code stream-json events into Event")


# Registry — string (from the spec) -> runner instance

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
