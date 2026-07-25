"""Supervised agent runner — the runner-agnostic core of the library.

Spawns an agent as an OS subprocess via an `AgentRunner` (opencode, Claude Code,
mock, …), supervises it by LIVENESS (not wall-clock), and reads its result from a
per-agent STATUS SIDECAR written by the agent itself.

Supervision model:
  - A background thread streams the runner's stdout through `runner.parse_event`;
    each real event resets an idle deadline.
  - The agent is killed ONLY when STALE: no event AND no sidecar for
    `idle_timeout_s`. An actively-emitting agent runs as long as it makes
    progress — there is no absolute cap.
  - Completion is detected the moment the sidecar appears on disk, so a
    done-but-lingering process is finished immediately.

Status is read from the agent-written sidecar (`<agent>.control.json`), written
by the agent via its Write tool — the control sidecar is the SOLE verdict. If it
is absent, the run is an error; the engine never inspects an agent's artifacts to
guess success. Any domain-specific check ("a report file was written") and any
flow routing ("re-run this stage", "resume at tech-stack") belong to the
orchestration layer's per-stage GATE, not here — this core supervises exactly one
subprocess and knows nothing about stages, artifacts, or the DAG.
Token/cost telemetry is harvested from the event stream into the result.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from agent_flow.core.control_protocol import build_control_preamble
from agent_flow.core.schema import ResultSchema, coerce_schema
from agent_flow.runners import AgentInvocation, AgentRunner, Event

# Liveness supervision.
#   IDLE = max silence (no runner event) before the agent is deemed STALE.
# This is the ONLY guard: an agent that keeps emitting events is alive and runs
# as long as it makes progress; one that has gone quiet for idle_timeout_s is
# hung -> kill. There is deliberately NO absolute cap — progress, not elapsed
# time, decides liveness. The default is generous because real LLM agents can
# pause 60-90s between tool calls (thinking, long writes); tune per run via the
# CLI --idle-timeout / AGENT_FLOW_IDLE_TIMEOUT_S, or per node via agent_node.
DEFAULT_IDLE_TIMEOUT_S = 120


class AgentTimeoutError(RuntimeError):
    """Raised when an agent goes STALE: no event and no sidecar for
    `idle_timeout_s`. This is a liveness timeout, not a wall-clock one — an
    actively-emitting agent is never killed regardless of elapsed time."""


class AgentContentFailedError(RuntimeError):
    """Raised when an agent reports a content failure via its control signal.

    This is a genuine failure the agent diagnosed itself (e.g. could not parse
    the report, missing required input). Retrying the same prompt will not help,
    so the retry policy must NOT retry this class.
    """


class AgentCrashError(RuntimeError):
    """Raised when an agent process exits non-zero with no error control signal.

    This represents a process-level crash (CLI error, OOM, rate-limit 429, …).
    It is transient and the retry policy SHOULD retry it.
    """


@dataclass
class AgentResult:
    """Outcome of one supervised agent run."""

    agent: str
    exit_code: int | None
    duration_s: float
    control: dict = field(default_factory=dict)  # status from the sidecar
    # Telemetry harvested from opencode's --format json event stream.
    tokens: int = 0
    cost: float = 0.0
    events: int = 0
    # How the run terminated: "completed" | "sidecar" | "stale" | "hard_cap".
    completion: str = "completed"
    # Result-schema validation outcome (only meaningful when a result_schema was
    # supplied). result_valid is True when no schema was given (nothing to fail).
    # result_obj is a Pydantic model INSTANCE when a PydanticSchema was used, else
    # None (a dict schema / no schema produce no new object — the dict is already
    # in control["result"]). A gate reads these — the engine never auto-fails.
    result_valid: bool = True
    result_obj: object = None
    result_errors: tuple[str, ...] = ()


@dataclass
class _Supervision:
    """Result of supervising a running agent process."""

    completion: str  # "completed" | "sidecar" | "stale"
    tokens: int
    cost: float
    events: int


def _start_line_reader(proc: subprocess.Popen):  # noqa: ANN201 - returns a Queue
    """Spawn a daemon thread that pushes each stdout line onto a queue.

    The reader isolates the (potentially blocking) readline from the
    supervision loop, so the loop can enforce the idle deadline even when the
    process emits nothing. A trailing None signals EOF.
    """
    assert proc.stdout is not None
    buf: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                buf.put(line)
        finally:
            buf.put(None)

    threading.Thread(target=_reader, daemon=True).start()
    return buf


def _apply_event(line: str, st: dict, runner: AgentRunner, on_event: Callable[[Event], None] | None) -> bool:
    """Fold one stdout line into supervision state via the runner's parser.

    Returns True if the line was a real event (a liveness heartbeat), False if
    it was noise (not counted toward liveness). If on_event is set, it is called
    with each real event for optional live display — guarded so a display error
    never disrupts supervision.
    """
    ev = runner.parse_event(line)
    if not ev.is_event:
        return False
    st["events"] += 1
    st["tokens"] += ev.tokens
    st["cost"] += ev.cost
    if ev.is_terminal:
        st["saw_terminal"] = True
    if on_event is not None:
        try:
            on_event(ev)
        except Exception:  # noqa: BLE001 - display must never break the run
            pass
    return True


def _supervise(
    proc: subprocess.Popen,
    *,
    runner: AgentRunner,
    idle_timeout_s: int,
    control_file: Path | None,
    on_event: Callable[[Event], None] | None = None,
) -> _Supervision:
    """Supervise a running agent by LIVENESS only — kill solely when idle.

    A background thread reads stdout; the runner parses each line into an Event.
    The main loop:
      - resets the IDLE deadline on every real event (the heartbeat),
      - stops early ("sidecar") the moment the control sidecar appears on disk
        — the agent's work is done even if the process lingers,
      - stops ("completed") when the reader hits EOF (process finished),
      - kills only on STALE: no event AND no sidecar for idle_timeout_s.

    Runner-agnostic: the only runner-specific step is `runner.parse_event`.
    """
    try:
        return _supervise_loop(proc, runner=runner, idle_timeout_s=idle_timeout_s, control_file=control_file, on_event=on_event)
    except KeyboardInterrupt:
        # Ctrl-C: kill the agent's whole process group before propagating, so we
        # never leave an orphaned opencode (and its MCP children) running.
        _kill_group(proc)
        raise


def _supervise_loop(
    proc: subprocess.Popen,
    *,
    runner: AgentRunner,
    idle_timeout_s: float,
    control_file: Path | None,
    on_event: Callable[[Event], None] | None,
) -> _Supervision:
    """The liveness loop (see _supervise). Extracted so _supervise stays a thin
    interrupt-handling wrapper (and both stay under the complexity limit)."""
    buf = _start_line_reader(proc)
    st = {"tokens": 0, "cost": 0.0, "events": 0, "saw_terminal": False}
    idle_deadline = time.monotonic() + idle_timeout_s

    def _finish(kind: str) -> _Supervision:
        _kill_group(proc)  # idempotent; ensures no lingering process
        return _Supervision(completion=kind, tokens=st["tokens"], cost=st["cost"], events=st["events"])

    def _sidecar_present() -> bool:
        return control_file is not None and control_file.exists()

    while True:
        now = time.monotonic()
        if _sidecar_present():
            return _finish("sidecar")  # work done (even if proc lingers)
        if now >= idle_deadline:
            return _finish("stale")  # silent for the whole idle window

        try:
            line = buf.get(timeout=max(min(idle_deadline - now, 1.0), 0.05))
        except queue.Empty:
            continue

        if line is None:  # reader EOF — process finished
            _reap(proc)
            return _Supervision(completion="completed", tokens=st["tokens"], cost=st["cost"], events=st["events"])

        # Line arrived: if it's a real event, it's a heartbeat -> reset idle.
        if _apply_event(line, st, runner, on_event):
            idle_deadline = time.monotonic() + idle_timeout_s
        if st["saw_terminal"] and _sidecar_present():
            return _finish("sidecar")


def _reap(proc: subprocess.Popen) -> None:
    """Wait briefly for a finished process; kill its group if it lingers."""
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        _kill_group(proc)


def _read_sidecar(control_file: Path | None) -> dict | None:
    """Read the agent-written control sidecar JSON, or None if absent/invalid.

    The sidecar is the agent's structured status receipt (written via its Write
    tool). Reading a file is deterministic — no stdout string-parsing, no
    code-fence fragility. Returns None when the file is missing or unparseable,
    which the caller treats as an error (no verdict was written).
    """
    if control_file is None or not control_file.exists():
        return None
    try:
        return json.loads(control_file.read_text())
    except json.JSONDecodeError, OSError:
        return None


def run_agent(
    *,
    agent: str,
    prompt: str,
    run_dir: Path,
    runner: AgentRunner,
    agent_dir: Path | None = None,
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
    model: str | None = None,
    instructions: str = "",
    env_extra: dict[str, str] | None = None,
    control_file: Path | None = None,
    result_schema: ResultSchema | dict | type | None = None,
    on_event: Callable[[Event], None] | None = None,
    shared_instructions: str = "",
    shared_context: str = "",
) -> AgentResult:
    """Run one agent as a supervised subprocess with a LIVENESS-based timeout.

    Supervision (all inside this function — the caller stays ignorant of it):
      - The agent is killed ONLY when STALE: no opencode event AND no sidecar
        for `idle_timeout_s`. An actively-working agent runs as long as it keeps
        emitting events — there is no absolute wall-clock cap.
      - Completion is detected by the CONTROL SIDECAR appearing on disk (or a
        terminal event), so a done-but-lingering process is finished immediately
        rather than waited on.

    Status is read from the agent-written sidecar (`control_file`) — the sole
    verdict. If the sidecar is absent, the run is an error (the engine does not
    inspect artifacts). Domain checks and flow routing live in the orchestration
    layer's per-stage gate, not here.
    Token/cost telemetry is harvested from the event stream into the result.

    Args:
        agent: agent name to dispatch.
        prompt: per-run work-order prompt.
        run_dir: the run's directory — where the control sidecar is written and
            the base for relative artifact paths. NOT a cwd concept and NOT where
            agent definitions live (that is `agent_dir`).
        runner: the AgentRunner strategy (opencode / mock / …) — owns command
            construction and stdout event parsing. This is the swappable seam.
        agent_dir: optional absolute directory where the runtime finds agent
            DEFINITIONS (opencode's `.opencode/agent/*.md`). Passed to the runner
            (opencode: `--dir <agent_dir>`), and used as the subprocess cwd. When
            None, the subprocess inherits the current cwd. Independent of run_dir.
        idle_timeout_s: max silence (no event, no sidecar) before "stale" -> kill.
        model: optional model override.
        instructions: resolved standing instructions (for runners without named
            agents; opencode ignores this — identity is in its .md).
        env_extra: extra env vars.
        control_file: path to the agent's status sidecar JSON — the verdict.
        result_schema: optional ResultSchema | JSON-schema dict | pydantic
            BaseModel subclass for the agent's `result` payload; injected into the
            prompt and validated (attached, never raised).
        on_event: optional callback invoked with each live runner Event, for
            display. The engine ignores it for supervision; display errors are
            swallowed so they can never disrupt the run.
        shared_instructions: optional run-wide instruction/brief injected into
            EVERY agent's prompt (after the control protocol, before the work
            order) — e.g. a global directive passed at orchestrator start.
        shared_context: optional run-wide context CONTENT (already read from
            files by the caller) injected into every agent's prompt, before
            shared_instructions — e.g. security rules / coding standards the
            agent must actually have, not merely be told to read.

    Raises:
        AgentTimeoutError: the agent went stale (idle) with no valid sidecar.
        AgentContentFailedError: sidecar reports status:error (do not retry).
    """
    run_dir.mkdir(parents=True, exist_ok=True)

    # Defensive: clear a stale sidecar so completion keys only on THIS run's
    # write. (Each agent already has its own per-agent sidecar path, so this
    # only matters across re-runs of the same agent.)
    if control_file is not None:
        control_file.unlink(missing_ok=True)

    # Compose the final prompt from string blocks. Order:
    #   [completion protocol] [run-wide context] [run-wide instructions] [prompt]
    # The protocol lives in ONE place (the library), not restated in every agent
    # .md. shared_context is ingested rules/standards CONTENT the agent must have
    # (before the free-text brief); shared_instructions is the run-wide brief.
    # The caller's `prompt` already contains any per-node context + instructions +
    # the work order (composed by agent_node). A result schema, if supplied, is
    # embedded in the protocol block.
    schema = coerce_schema(result_schema)
    blocks: list[str] = []
    if control_file is not None:
        schema_dict = schema.to_json_schema() if schema is not None else None
        blocks.append(build_control_preamble(agent, str(control_file), schema_dict))
    if shared_context and shared_context.strip():
        blocks.append(f"## Run-wide context\n\n{shared_context.strip()}")
    if shared_instructions and shared_instructions.strip():
        blocks.append(f"## Run-wide instructions\n\n{shared_instructions.strip()}")
    blocks.append(prompt)
    full_prompt = "\n\n".join(blocks)

    cmd = runner.build_command(
        AgentInvocation(agent=agent, prompt=full_prompt, model=model, instructions=instructions, agent_dir=str(agent_dir) if agent_dir else "")
    )

    env = os.environ.copy()
    if env_extra:
        env.update(env_extra)

    # cwd = agent_dir when set (the runtime's project dir, e.g. where opencode
    # finds .opencode/agent). Not run_dir — that is the artifact/sidecar root and
    # nothing chdir's there. When unset, inherit the current cwd.
    start = time.monotonic()
    proc = subprocess.Popen(
        cmd,
        cwd=str(agent_dir) if agent_dir else None,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )

    sup = _supervise(proc, runner=runner, idle_timeout_s=idle_timeout_s, control_file=control_file, on_event=on_event)
    duration = time.monotonic() - start

    # The control sidecar is the SOLE verdict. No sidecar => error; the engine
    # never inspects artifacts to guess success (that is the gate's job, one
    # layer up).
    sidecar = _read_sidecar(control_file)
    if sidecar is None:
        sidecar = {
            "status": "error",
            "agent": agent,
            "reason": f"no control sidecar written (completion={sup.completion})",
        }

    # Validate the result payload against the schema, if one was supplied. The
    # outcome is ATTACHED (never raised) — a schema violation is a flow-control
    # decision for the gate, not an engine failure.
    outcome = schema.validate(sidecar.get("result", {})) if schema is not None else None

    result = AgentResult(
        agent=agent,
        exit_code=proc.returncode,
        duration_s=duration,
        control=sidecar,
        tokens=sup.tokens,
        cost=sup.cost,
        events=sup.events,
        completion=sup.completion,
        result_valid=outcome.valid if outcome is not None else True,
        result_obj=outcome.obj if outcome is not None else None,
        result_errors=outcome.errors if outcome is not None else (),
    )

    status = sidecar.get("status")

    # Stale (idle) with no successful sidecar => genuinely hung.
    if sup.completion == "stale" and status not in ("ok", "verified"):
        raise AgentTimeoutError(f"agent {agent!r} went stale after {duration:.1f}s (no event/sidecar for {idle_timeout_s}s)")

    # Content failure: sidecar explicitly reports a non-ok status.
    if status not in ("ok", "verified"):
        raise AgentContentFailedError(f"agent {agent!r} reported status={status!r}: {sidecar.get('reason')}")

    return result


def _kill_group(proc: subprocess.Popen) -> None:
    """Terminate the child's whole process group: SIGTERM, grace, then SIGKILL.

    #1 fix: after exhausting both signals, falls back to a direct proc.kill()
    so we never leave a zombie regardless of whether os.killpg reached the
    group leader.
    """
    try:
        pgid = os.getpgid(proc.pid)
    except ProcessLookupError:
        return  # process already gone

    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            return  # group already gone — nothing to do
        try:
            proc.wait(timeout=5)
            return  # process exited cleanly after signal
        except subprocess.TimeoutExpired:
            continue  # escalate to next signal

    # Last resort: direct kill on the process leader in case os.killpg missed it.
    try:
        proc.kill()
        proc.wait(timeout=5)
    except ProcessLookupError, subprocess.TimeoutExpired:
        pass
