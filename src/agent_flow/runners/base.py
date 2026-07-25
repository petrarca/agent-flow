"""Runner seam — the neutral contract every agent runtime implements.

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

This module holds the NEUTRAL contract only (the Protocol + the shared data
types); each concrete runtime lives in its own sibling module (opencode.py,
mock.py, claude_code.py). The seam is a `typing.Protocol` (structural): a runner
matches by SHAPE and does not inherit — there is no shared implementation to
hoist (build_command / parse_event are entirely runtime-specific), so a Protocol
is the right tool. Contrast the backend seam, which DOES share logic and uses an
ABC base.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, Field

# Model contract. The library NEVER hardcodes a model. When no model is
# configured (param/env/CLI/programmatic), the runner omits --model so the
# runtime resolves it from its own config/provider/router. A model is passed
# through ONLY when explicitly set.
#
# The library's guarantee stops there: "no invented model; an explicit value is
# passed through". It does NOT guarantee the explicit model WINS — final
# precedence is the runtime's own. On opencode a CLI --model beats config; on
# Claude Code a managed/MDM policy setting outranks CLI args and can override
# --model. So model precedence is a per-runner property owned by the runner, not
# a cross-runtime invariant. Do not build upper-layer logic assuming --model is
# final.


class AgentRunnerInfo(BaseModel):
    """Diagnostic self-description of a runner's runtime (best-effort).

    Returned by the optional `AgentRunner.info()` for a doctor/summary view — it
    reports what the RUNTIME says about itself, never library-invented values. A
    field is left None/[] when the runtime cannot be introspected — either
    transiently (binary missing, command failed) or STRUCTURALLY, when the
    runtime exposes no way to resolve it from a CLI. E.g. opencode reports the
    resolved model/tools via `opencode debug config`, but Claude Code has no
    equivalent CLI subcommand (resolution is SDK-only), so its info() may report
    version/availability only and leave resolved_model=None, tools=[]. A None
    field therefore means "not introspectable here", not "misconfigured".
    info() must never raise.
    """

    name: str = Field(description="Runner id (e.g. 'opencode', 'mock', 'claude').")
    available: bool = Field(default=False, description="Is the runtime usable (binary found / importable)?")
    version: str | None = Field(default=None, description="Runtime version, if it reports one.")
    resolved_model: str | None = Field(default=None, description="The model the RUNTIME would actually use (its own default); None if unknown.")
    tools: list[str] = Field(default_factory=list, description="Tools / MCP servers the runtime exposes, if it can report them.")
    detail: str = Field(default="", description="Freeform notes (path, config source, error hints).")


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
    """One parsed unit of runner stdout: liveness + telemetry + a NEUTRAL view.

    Two audiences, cleanly separated so nothing above the runner knows the
    runtime's wire format:

    - SUPERVISION (engine): tokens / cost / is_terminal / is_event. Unchanged.

    - DISPLAY (CLI): a runner-AGNOSTIC, already-normalized view of the event —
      `kind` + `title` + `detail` + `status`. The RUNNER fills these from its own
      (versioned, runtime-specific) schema; the CLI renders them with styling and
      never re-parses `raw`. This is the seam: "how to read the stream" lives in
      the runner, "how to lay it out" lives in the CLI, and they meet on these
      neutral fields. A new runtime (Claude Code, …) populates the same fields
      from its own stream and the existing CLI renders it with zero changes.

    - DIAGNOSTIC: `raw` still carries the ORIGINAL stdout line verbatim, for
      --show-events deep debugging and for runtimes whose renderer wants it. It
      is a passthrough, NOT something the neutral renderer interprets.

    kind    neutral category the CLI styles: "step_start" | "step_end" | "tool" |
            "text" | "other" ("" when this is not a displayable event).
    title   primary human summary (tool title/target, the message text, …).
    detail  secondary hint (tool metadata: "12 matches", "exit 0", a diff stat).
    status  tool lifecycle for coloring: "running" | "completed" | "error" | "".
    For a file-changing tool (edit/write), three neutral, structured fields carry
    the change — the runner MAPS its runtime's native shape onto them (it does not
    parse or compute): opencode `metadata.diff` + `filediff.{additions,deletions}`,
    Claude Code `gitDiff.{patch,additions,deletions}`. The CLI is a dumb renderer:
    it formats `+added/-removed` for the one-line detail and renders `diff` as a
    syntax-highlighted block only under --show-diffs.

    diff    unified-diff patch text (or "").
    added   lines added (0 if none/unknown).
    removed lines removed (0 if none/unknown).
    """

    tokens: int = 0
    cost: float = 0.0
    is_terminal: bool = False
    is_event: bool = True  # False for lines that are not real events (ignored for liveness)
    raw: str = ""  # the original stdout line, diagnostic passthrough (NOT for neutral render)
    # Neutral display view (runner-filled, CLI-rendered) — see class docstring.
    kind: str = ""
    title: str = ""
    detail: str = ""
    status: str = ""
    diff: str = ""
    added: int = 0
    removed: int = 0

    @staticmethod
    def none() -> Event:
        return Event(is_event=False)


@runtime_checkable
class AgentRunner(Protocol):
    """Strategy for one agent-execution backend.

    REQUIRED: `build_command` (the argv) + `parse_event` (stdout line -> Event).
    Everything else (supervision, kill, sidecar, DAG) is runner-agnostic.

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

    def build_command(self, inv: AgentInvocation) -> list[str]:
        """The argv to spawn this runner for one agent invocation."""
        ...

    def parse_event(self, line: str) -> Event:
        """Parse one stdout line into an Event (liveness + tokens/cost)."""
        ...
