"""Neutral data types and contracts shared by all execution paths.

This module is the COMMON FOUNDATION for the two-level execution seam:

  AgentInvocation  the complete, runtime-neutral request to run one agent.
                   Input to AgentExecutor.run (see runners/executor.py).
  AgentResult      the outcome — control envelope + telemetry + typed object.
                   Output from AgentExecutor.run (defined in executor.py).
  compose_prompt   assemble the runtime-neutral prompt (shared_context +
                   shared_instructions + per-node prompt) in one place.
  DEFAULT_IDLE_TIMEOUT_S  the liveness/timeout budget default.

Two execution seams consume these types:

  AgentExecutor (ABC, executor.py)
      The HIGH-LEVEL seam: "run one invocation, produce a result."
      - SubprocessExecutor (core/agent_runtime.py) — spawns a CLI runtime,
        supervises by liveness, reads a control SIDECAR. Delegates the two
        subprocess wire details to an AgentRunner (this module).
      - InProcessExecutor (runners/inprocess.py) — calls a Python function
        directly; no subprocess, sidecar, or control preamble.

  AgentRunner (Protocol, this module)
      The LOW-LEVEL seam: "how to talk to one subprocess runtime."
      Owned by SubprocessExecutor; NOT the public seam.
      - build_command(inv) -> argv
      - parse_event(line)  -> Event (liveness + telemetry)
      Each concrete runtime (opencode.py, claude_code.py) implements exactly
      these two methods. A Protocol (structural, not ABC) because there is no
      shared implementation to hoist. (Mock is not a runner — it is MockExecutor,
      selected by the --mock-agents mode.)

The control sidecar is SubprocessExecutor's PRIVATE mechanism — it is written
by CLI agents via their Write tool and read by SubprocessExecutor. It is not
part of AgentRunner, AgentInvocation, or the AgentExecutor contract.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, Field

# Liveness / timeout budget default (seconds). Owned here (the neutral runner
# contract) because it is a field default on AgentInvocation. The subprocess
# executor treats it as an idle deadline; an in-process executor may use it as a
# wall-clock cap hint. agent_runtime re-exports it for backward compatibility.
DEFAULT_IDLE_TIMEOUT_S = 120

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
    """The complete, runtime-NEUTRAL request to run ONE agent — the seam's input.

    This is the single input to an `AgentExecutor.run(inv) -> AgentResult`. It
    carries EVERYTHING an executor needs, and NOTHING that is specific to one
    execution mechanism: a subprocess executor and an in-process executor receive
    the exact same invocation. (The subprocess control sidecar is deliberately
    NOT here — it is a SubprocessExecutor-private detail derived from run_dir +
    agent, along with the control preamble it injects.)

    Prompt layering. `prompt` is the fully-composed PER-NODE prompt (per-node
    context + instructions + the one-time instruction + the work order — already
    joined by agent_node). `shared_context` / `shared_instructions` are the
    RUN-WIDE blocks, kept separate so a single composer (`compose_prompt`) lays
    the final order in ONE place: [shared_context][shared_instructions][prompt].
    A subprocess executor additionally prepends the control preamble (its own
    mechanism); an in-process executor uses the composed prompt as-is.

    Agent identity. `agent` is the logical name/ref; `instructions` is the
    resolved standing context for runtimes WITHOUT named agents (opencode ignores
    it — its identity lives in its .md; Claude Code injects it as a system
    prompt). Executors materialise identity their own way from these fields.

    Text AND data. A subprocess agent can only be handed TEXT, so `prompt` is the
    contract that matters for it. An IN-PROCESS agent is Python calling Python and
    wants the values themselves, so the same request is also carried structured:
    `inputs` (this node's resolved work order) and `params` (the run's domain
    params). Both are already templated — the exact values that were rendered into
    the prompt — so an impl never has to parse them back out of the text it was
    given. The subprocess path simply ignores them.
    """

    agent: str  # logical agent name / ref
    prompt: str  # fully-composed per-node prompt (context+instructions+one-time+work order)
    run_dir: Path  # the run's directory (artifact/sidecar root; base for relative paths)
    node: str = ""  # the NODE this invocation runs (neutral identity; unique per run).
    # Used e.g. by SubprocessExecutor to key its per-node control sidecar
    # ("<node>.control.json"). Falls back to `agent` when empty.
    result_schema: object = None  # ResultSchema | JSON-schema dict | pydantic model; typed output contract
    model: str | None = None
    agent_dir: str = ""  # absolute dir where agent DEFINITIONS live (opencode: --dir); "" = runtime default
    instructions: str = ""  # resolved standing instructions (for runners without named agents)
    shared_instructions: str = ""  # run-wide brief injected into every agent
    shared_context: str = ""  # run-wide context CONTENT (already read from files)
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S  # liveness budget (subprocess) / cap hint (in-process)
    on_event: Callable[[Event], None] | None = None  # live progress callback (both kinds may emit)
    # The STRUCTURED twin of `prompt` (see "Text AND data" above). Both are
    # snapshots owned by this invocation — an impl may read them freely.
    inputs: dict[str, str] = field(default_factory=dict)  # this node's resolved work order ({KEY: value}, templated)
    input_obj: object = None  # `inputs` validated against the node's input_schema (a pydantic instance), else None
    params: dict[str, Any] = field(default_factory=dict)  # the run's domain params (incl. upstream `exports`)


def compose_prompt(inv: AgentInvocation) -> str:
    """Assemble the runtime-neutral prompt for an invocation, in ONE place.

    Order: [run-wide context][run-wide instructions][per-node prompt]. The
    per-node `prompt` already contains, in order, the node's context +
    instructions + the one-time instruction + the work order (composed by
    agent_node). This helper only prepends the RUN-WIDE blocks, so the full
    top-to-bottom prompt order lives here rather than being split across layers.

    The subprocess control preamble is NOT added here — it is subprocess-specific
    (it instructs a CLI agent to write a control sidecar) and is prepended by
    SubprocessExecutor. An in-process executor uses this composed prompt as-is.
    """
    blocks: list[str] = []
    if inv.shared_context and inv.shared_context.strip():
        blocks.append(f"## Run-wide context\n\n{inv.shared_context.strip()}")
    if inv.shared_instructions and inv.shared_instructions.strip():
        blocks.append(f"## Run-wide instructions\n\n{inv.shared_instructions.strip()}")
    blocks.append(inv.prompt)
    return "\n\n".join(blocks)


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
    # A RUNTIME ERROR the runner recognised in the stream (e.g. opencode's
    # {"type":"error"} line: model unresolved, provider failure). "" when the
    # event is not an error. The supervisor collects these so that a run which
    # ends with no control sidecar can report WHY, not just "no sidecar written".
    error: str = ""
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


@dataclass(frozen=True)
class LaunchSpec:
    """How to launch one agent invocation — the runner's `build_command` output.

    A runner turns a (prompt-composed) `AgentInvocation` into this control
    structure. It separates the two things the executor needs but must NOT
    conflate:

      argv            the exact argv to spawn (Popen consumes it). Includes
                      whatever payload the runner chose — for a CLI runner the
                      prompt is usually the trailing positional.
      display         a human, diagnosis-safe one-line rendering of the command
                      with the (huge) prompt payload ELIDED. The runner formats
                      it because only the runner knows which parts are flags vs.
                      payload; the executor just prints it verbatim in an error.
                      A plain string, not an argv.
      capture_stderr  when True the executor captures stderr on a separate pipe
                      and passes each line to the runner's `parse_stderr_line`
                      (if implemented). False → stderr is merged into stdout
                      (the default, backward-compatible behaviour). Runners that
                      emit structured diagnostics on stderr (e.g. opencode with
                      --print-logs) set this to True so the executor can extract
                      actionable error detail without polluting the stdout parse.
    """

    argv: list[str]
    display: str
    capture_stderr: bool = False


# Execution transports. A runner declares HOW an executor talks to it:
#   "subprocess" — spawn a CLI process, stream stdout (SubprocessExecutor).
#   "http-sse"   — POST to a running daemon, stream SSE events (ServeExecutor).
# The transport decides WHICH executor a runner pairs with; the registry reads it
# from the runner's spec so executor selection never string-matches names.
TRANSPORT_SUBPROCESS = "subprocess"
TRANSPORT_HTTP_SSE = "http-sse"

# Execution modes — the human-facing axis orthogonal to the agent runtime:
#   "process" — spawn a fresh CLI process per invocation.
#   "remote"  — run against a shared, already-running daemon over the network.
MODE_PROCESS = "process"
MODE_REMOTE = "remote"


@dataclass(frozen=True)
class RunnerSpec:
    """Static, declared self-description of a runner — identity + requirements.

    Every runner returns one from `spec()`. This is the STATIC identity of a
    runner (always available, never does I/O, never fails), as opposed to
    `AgentRunnerInfo` from `info()` which is BEST-EFFORT runtime introspection
    (version/resolved-model, may be None, may fail). The registry, preflight, and
    executor selection all read this instead of parsing names or guessing from
    suffixes.

    runtime         the agent runtime family — "opencode", "goose", "crush",
                    "claude". Two runners of the same runtime (process + remote)
                    share this and their per-runtime knowledge module.
    mode            "process" | "remote" — the execution axis (MODE_*).
    transport       "subprocess" | "http-sse" — how an executor talks to this
                    runner (TRANSPORT_*). Decides which executor it pairs with.
    needs_endpoint  True when the runner requires a serve_url (a daemon endpoint).
                    Preflight and executor construction read this generically —
                    no "-remote" suffix matching anywhere.
    name            the primary registry key (e.g. "opencode", "opencode-remote").
    aliases         additional registry keys resolving to the same runner
                    (e.g. ("opencode-http",) for the remote runner).
    """

    runtime: str
    mode: str
    transport: str
    name: str
    needs_endpoint: bool = False
    aliases: tuple[str, ...] = ()


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

      - `build_verdict_preamble(agent, control_file, result_schema) -> str`
        (OPTIONAL): the completion-protocol instruction block prepended to the
        prompt. A sidecar-style runner returns the "write CONTROL_FILE" block; a
        structured-output runner returns a "return your final structured output"
        block. Pure/stateless — no I/O. When a runner does NOT implement it, the
        executor falls back to the shared `build_control_preamble` (sidecar).

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
