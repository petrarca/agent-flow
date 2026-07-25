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
import shutil
from dataclasses import dataclass
from pathlib import Path
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
        # Only force a model when one was explicitly configured (param/env/CLI/
        # programmatic). With no model, OMIT --model so opencode resolves it from
        # its own config/router — the library never hardcodes a model.
        cmd = ["opencode", "run", "--agent", inv.agent, "--format", "json", "--auto"]
        if inv.model:
            cmd += ["--model", inv.model]
        # --dir points opencode at the project where .opencode/agent lives, so
        # agents resolve regardless of the process cwd (opencode chdir's into it).
        if inv.agent_dir:
            cmd += ["--dir", inv.agent_dir]
        cmd.append(inv.prompt)
        return cmd

    def parse_event(self, line: str) -> Event:
        """Parse one opencode NDJSON line into supervision fields + the NEUTRAL view.

        This is the ONE place opencode's wire shape is interpreted. Besides the
        telemetry the engine needs (tokens/cost/is_terminal), we normalize the
        event into the runner-agnostic display fields (kind/title/detail/status)
        so the CLI can render it WITHOUT ever re-parsing opencode JSON. `raw` is
        still carried verbatim for --show-events / diagnostics.
        """
        stripped = line.strip()
        if not stripped.startswith("{"):
            return Event.none()
        try:
            ev = json.loads(stripped)
        except json.JSONDecodeError:
            return Event.none()
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        ptype = part.get("type") or ev.get("type") or "event"

        if ptype in ("step-start", "step_start"):
            return Event(raw=stripped, kind="step_start")
        if ptype in ("step-finish", "step_finish"):
            tokens = int((part.get("tokens") or {}).get("total") or 0)
            cost = float(part.get("cost") or 0.0)
            return Event(tokens=tokens, cost=cost, is_terminal=part.get("reason") == "stop", raw=stripped, kind="step_end")
        if ptype == "tool":
            title, detail, status = _opencode_tool_view(part)
            return Event(raw=stripped, kind="tool", title=title, detail=detail, status=status)
        if ptype == "text":
            text = " ".join((part.get("text") or "").split())
            return Event(raw=stripped, kind="text", title=text)
        # A real event we don't specially render: keep it displayable via `title`.
        return Event(raw=stripped, kind="other", title=str(ptype))

    def preflight_checks(self, agent_dir: str | Path | None) -> list:
        """opencode-specific pre-conditions: binary on PATH, not nested, agent layout."""
        from agent_flow.preflight import Check

        checks: list[Check] = [_opencode_installed_check(), _not_nested_check()]
        # opencode resolves agents from `<agent_dir>/.opencode/agent*`.
        if agent_dir:
            oc = Path(agent_dir) / ".opencode"
            if not (oc.is_dir() and any(oc.glob("agent*"))):
                checks.append(Check("opencode-agent-layout", False, True, f"no .opencode/agent* under {agent_dir} (opencode --dir target)."))
            else:
                checks.append(Check("opencode-agent-layout", True, True, f"agent definitions at {oc}"))
        return checks

    def info(self, agent_dir: str | Path | None = None) -> AgentRunnerInfo:
        """Best-effort diagnostics: opencode version + its config AS RESOLVED IN agent_dir.

        opencode resolves its config (model, MCP tools, providers) relative to the
        directory it runs in (it walks up from --dir to the git root, merging
        opencode.json + global). So the resolved model and tools are a PER-DIR
        (hence per-node) property: the same runner in a different agent_dir can see
        a different model and a different tool set. We therefore introspect with
        cwd=agent_dir. With no agent_dir, we report only version/availability.
        """
        path = shutil.which("opencode")
        if not path:
            return AgentRunnerInfo(name=self.name, available=False, detail="opencode not found on PATH")
        version = _run_text(["opencode", "--version"])
        if not agent_dir:
            return AgentRunnerInfo(
                name=self.name, available=True, version=version, detail=f"found at {path}; pass agent_dir for resolved model/tools"
            )
        cfg = _opencode_debug_config(cwd=str(agent_dir))
        model = cfg.get("model")
        tools = sorted(k for k, v in (cfg.get("mcp") or {}).items() if isinstance(v, dict) and v.get("enabled"))
        return AgentRunnerInfo(
            name=self.name,
            available=True,
            version=version,
            resolved_model=model,
            tools=tools,
            detail=f"found at {path}; config resolved in {agent_dir}",
        )


# Per-tool input field carrying the human "target" (the file, command, pattern,
# url, …). opencode also sets `state.title` which we prefer; this is the fallback
# for tools/versions that don't. Ordered by likelihood so the first present wins.
_OPENCODE_TOOL_TARGET_KEYS = ("filePath", "command", "pattern", "url", "path", "query", "name", "description")

# Compact per-tool metadata hint (opencode's state.metadata), rendered as a
# secondary "detail". Only a few scalar keys are worth one line.
_OPENCODE_TOOL_META_KEYS = (("matches", "matches"), ("count", "matches"), ("exit", "exit"))


def _opencode_tool_view(part: dict) -> tuple[str, str, str]:
    """Normalize an opencode `tool` part to neutral (title, detail, status).

    title  = "<tool> <target>" — prefer opencode's own state.title; else the
             tool name plus the first present target input field.
    detail = a compact metadata hint ("12 matches", "exit 0") when available.
    status = "running" | "completed" | "error" | "" (from state.status/error).
    """
    tool = part.get("tool", "tool")
    state = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = state.get("input") if isinstance(state.get("input"), dict) else {}
    meta = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}

    # Build the "target" part: opencode's own state.title if present (grep->the
    # pattern, edit->"Edit <file>", …), else the first present input field.
    target = (state.get("title") or "").strip()
    if not target:
        target = next((str(inp[k]) for k in _OPENCODE_TOOL_TARGET_KEYS if inp.get(k)), "")

    # Always LEAD with the tool name so every tool line reads uniformly
    # ("read <path>", "grep <pattern>", "edit <file>"). opencode's state.title is
    # inconsistent — for some tools it already embeds the verb (edit -> "Edit
    # <file>"), for others it is just the bare target (read -> "<path>"). Only
    # prefix the tool name when the target does not already start with it, so we
    # don't produce "edit Edit <file>".
    if target and target.lower().startswith(tool.lower()):
        title = target
    else:
        title = f"{tool} {target}".rstrip()

    detail = _opencode_tool_detail(meta)

    status = str(state.get("status") or "")
    if state.get("error"):
        status = "error"
    return title, detail, status


def _opencode_tool_detail(meta: dict) -> str:
    """A compact secondary hint from an opencode tool's state.metadata, or "".

    Covers the few metadata shapes worth one line: grep/glob match counts, bash
    exit codes, and an edit's diff stat (+adds/-dels from metadata.filediff).
    """
    # edit: a "+A/-D" diff stat is the most useful hint for a write-shaped tool.
    filediff = meta.get("filediff") if isinstance(meta.get("filediff"), dict) else None
    if filediff:
        adds = filediff.get("additions")
        dels = filediff.get("deletions")
        if isinstance(adds, (int, float)) or isinstance(dels, (int, float)):
            return f"+{int(adds or 0)}/-{int(dels or 0)}"
    # grep/glob match counts, bash exit codes.
    for src_key, label in _OPENCODE_TOOL_META_KEYS:
        val = meta.get(src_key)
        if isinstance(val, (int, float)):
            return f"{int(val)} {label}" if label == "matches" else f"{label} {int(val)}"
    return ""


def _run_text(cmd: list[str], timeout: float = 10.0) -> str | None:
    """Run a command and return its stripped stdout first line, or None on any error."""
    import subprocess

    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except OSError, subprocess.SubprocessError:
        return None
    line = (out.stdout or "").strip().splitlines()
    return line[0].strip() if line else None


def _opencode_debug_config(cwd: str | None = None) -> dict:
    """`opencode debug config` (run in `cwd`) parsed to a dict; {} on any failure.

    Run with cwd=agent_dir: opencode resolves config relative to where it runs
    (there is no --dir flag on `debug config`), so the cwd determines the model
    and enabled MCP tools reported.
    """
    import subprocess

    try:
        out = subprocess.run(["opencode", "debug", "config"], capture_output=True, text=True, timeout=15.0, check=False, cwd=cwd)
        return json.loads(out.stdout)
    except OSError, subprocess.SubprocessError, json.JSONDecodeError:
        return {}


def _opencode_installed_check():
    from agent_flow.preflight import Check

    path = shutil.which("opencode")
    if path:
        return Check("opencode-installed", True, True, f"found at {path}")
    return Check("opencode-installed", False, True, "opencode not found on PATH. Install it and ensure `opencode` is executable.")


def _not_nested_check():
    import os

    from agent_flow.preflight import Check

    if os.environ.get("OPENCODE") == "1":
        return Check(
            "not-nested-session", False, True, "Running inside an opencode session (OPENCODE=1). Start from a normal shell, outside opencode."
        )
    return Check("not-nested-session", True, True, "not inside an opencode session")


# mock (no LLM, no tokens) — used for token-free demos/tests


class MockRunner:
    """Local stub runner: spawns the packaged _mock_agent.py; no event stream."""

    name = "mock"

    def __init__(self, stub: Path | None = None) -> None:
        # _mock_agent.py ships inside the package (sibling of this module).
        self._stub = stub or (Path(__file__).resolve().parent / "_mock_agent.py")

    def build_command(self, inv: AgentInvocation) -> list[str]:
        cmd = ["python3", str(self._stub), "--agent", inv.agent, "--prompt", inv.prompt]
        if inv.model:
            cmd += ["--model", inv.model]
        return cmd

    def parse_event(self, line: str) -> Event:
        return Event.none()  # mock finishes fast; completion is via sidecar

    def info(self, agent_dir: str | Path | None = None) -> AgentRunnerInfo:
        return AgentRunnerInfo(name=self.name, available=True, detail="local no-token stub; no model, no external tools")


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

    info() note: Claude Code has NO CLI subcommand that prints resolved config
    (opencode's `debug config` has no equivalent). Resolution is SDK-only
    (`resolveSettings({cwd})`, alpha). So info() should report version/available
    from `claude --version` and leave resolved_model=None, tools=[] (or call the
    Agent SDK if a dependency on it is later accepted). Config resolves relative
    to cwd here too (.claude/settings.local.json > project > user), with a
    managed/MDM policy tier ABOVE CLI args — hence the model-precedence caveat in
    the module header.
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
