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
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, replace
from pathlib import Path

from agent_flow.core.control_protocol import build_control_preamble
from agent_flow.core.schema import ResultSchema, coerce_schema
from agent_flow.runners import AgentInvocation, AgentRunner, Event
from agent_flow.runners.base import DEFAULT_IDLE_TIMEOUT_S, compose_prompt
from agent_flow.runners.executor import AgentCrashError, AgentExecutor, AgentResult, AgentTimeoutError

# How many trailing raw stdout lines to keep for a no-sidecar diagnostic.
_TAIL_LINES = 20

# Liveness supervision.
#   IDLE = max silence (no runner event) before the agent is deemed STALE.
# This is the ONLY guard: an agent that keeps emitting events is alive and runs
# as long as it makes progress; one that has gone quiet for idle_timeout_s is
# hung -> kill. There is deliberately NO absolute cap — progress, not elapsed
# time, decides liveness. The default is generous because real LLM agents can
# pause 60-90s between tool calls (thinking, long writes); tune per run via the
# CLI --idle-timeout / AGENT_FLOW_IDLE_TIMEOUT_S, or per node via agent_node.
# The constant is defined in runners.base (it is a field default on
# AgentInvocation) and re-imported here so existing `from ...agent_runtime import
# DEFAULT_IDLE_TIMEOUT_S` references keep working.


@dataclass
class _Supervision:
    """Result of supervising a running agent process."""

    completion: str  # "completed" | "sidecar" | "stale"
    tokens: int
    cost: float
    events: int
    # Diagnostics for a run that ends with NO control sidecar: runtime errors the
    # runner recognised in the stream (e.g. opencode's {"type":"error"}), and a
    # bounded tail of the last raw stdout lines. Empty on a clean run.
    errors: tuple[str, ...] = ()
    tail: tuple[str, ...] = ()


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


def _start_stderr_reader(proc: subprocess.Popen):  # noqa: ANN201 - returns a Queue
    """Spawn a daemon thread that drains stderr onto a queue (same as stdout reader).

    Used when LaunchSpec.capture_stderr is True. The runner's parse_stderr_line
    is called per line in the supervision loop — only when something went wrong
    (the runner controls which lines are actionable). A trailing None signals EOF.
    """
    assert proc.stderr is not None
    buf: queue.Queue[str | None] = queue.Queue()

    def _reader() -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
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
    if ev.error:
        st["errors"].append(ev.error)
    if on_event is not None:
        try:
            on_event(ev)
        except Exception:  # noqa: BLE001 - display must never break the run
            pass
    return True


def _consume_line(line: str, st: dict, runner: AgentRunner, on_event: Callable[[Event], None] | None) -> bool:
    """Record a raw line's tail, then fold it in via `_apply_event`.

    Keeps the no-sidecar diagnostic (bounded tail of raw output) next to event
    folding so the supervision loop stays a simple liveness loop. Returns whether
    the line was a real event (a liveness heartbeat).
    """
    if line.strip():
        st["tail"].append(line.rstrip("\n"))
    return _apply_event(line, st, runner, on_event)


def _sidecar_probe(control_file: Path | None) -> Callable[[], bool]:
    """A cheap 'has the control sidecar appeared?' predicate for the loop."""
    return lambda: control_file is not None and control_file.exists()


def _stop_kind(idle: bool, sidecar: bool) -> str | None:
    """Map the two pre-line stop conditions to a completion kind (or None)."""
    if sidecar:
        return "sidecar"  # work done, even if the process lingers
    if idle:
        return "stale"  # silent for the whole idle window
    return None


def _finish(proc: subprocess.Popen, st: dict, kind: str, *, kill: bool) -> _Supervision:
    """Build the _Supervision result, optionally killing a lingering process."""
    if kill:
        _kill_group(proc)  # idempotent; ensures no lingering process
    return _Supervision(
        completion=kind,
        tokens=st["tokens"],
        cost=st["cost"],
        events=st["events"],
        errors=tuple(st["errors"]),
        tail=tuple(st["tail"]),
    )


def _drain_stderr(stderr_buf: queue.Queue | None, runner: AgentRunner, errors: list) -> None:
    """Non-blocking drain of any stderr lines already queued.

    Called at the end of the supervision loop (after EOF or kill) to flush
    whatever the runner's stderr reader has buffered. Each line is passed to
    runner.parse_stderr_line (if implemented); non-None results are appended to
    `errors` so _no_verdict_reason can include the real cause.

    Non-blocking: only drains what is already in the queue — never waits. The
    stderr reader thread is a daemon and will have written all lines before the
    process exits, so by the time we call this the queue is complete.
    """
    if stderr_buf is None:
        return
    parse_stderr = getattr(runner, "parse_stderr_line", None)
    if parse_stderr is None:
        return
    while True:
        try:
            line = stderr_buf.get_nowait()
        except queue.Empty:
            break
        if line is None:
            break
        msg = parse_stderr(line)
        if msg:
            errors.append(msg)


def _supervise(
    proc: subprocess.Popen,
    *,
    runner: AgentRunner,
    idle_timeout_s: int,
    control_file: Path | None,
    on_event: Callable[[Event], None] | None = None,
    stderr_buf: queue.Queue | None = None,
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
    stderr_buf: when set (LaunchSpec.capture_stderr=True), drained at completion
    via runner.parse_stderr_line; extracted errors fold into _Supervision.errors.
    """
    try:
        return _supervise_loop(
            proc, runner=runner, idle_timeout_s=idle_timeout_s, control_file=control_file, on_event=on_event, stderr_buf=stderr_buf
        )
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
    stderr_buf: queue.Queue | None,
) -> _Supervision:
    """The liveness loop (see _supervise). Extracted so _supervise stays a thin
    interrupt-handling wrapper (and both stay under the complexity limit)."""
    buf = _start_line_reader(proc)
    st: dict = {"tokens": 0, "cost": 0.0, "events": 0, "saw_terminal": False, "errors": [], "tail": deque(maxlen=_TAIL_LINES)}
    idle_deadline = time.monotonic() + idle_timeout_s
    sidecar_present = _sidecar_probe(control_file)

    while True:
        # Stop conditions checked before waiting for the next line.
        stop = _stop_kind(time.monotonic() >= idle_deadline, sidecar_present())
        if stop is not None:
            _drain_stderr(stderr_buf, runner, st["errors"])
            return _finish(proc, st, stop, kill=True)

        try:
            line = buf.get(timeout=max(min(idle_deadline - time.monotonic(), 1.0), 0.05))
        except queue.Empty:
            continue

        if line is None:  # reader EOF — process finished
            _reap(proc)
            _drain_stderr(stderr_buf, runner, st["errors"])
            return _finish(proc, st, "completed", kill=False)

        # Line arrived: fold it in (tail + events + errors). A real event is a
        # heartbeat -> reset the idle deadline. A terminal event once the sidecar
        # is on disk stops promptly (else the top-of-loop check catches it next).
        if _consume_line(line, st, runner, on_event):
            idle_deadline = time.monotonic() + idle_timeout_s
        if st["saw_terminal"] and sidecar_present():
            _drain_stderr(stderr_buf, runner, st["errors"])
            return _finish(proc, st, "sidecar", kill=True)


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


def _no_verdict_reason(sup: _Supervision, returncode: int | None, display: str, cwd: str) -> str:
    """Build a user-facing `reason` for a run that produced NO status verdict.

    Source-neutral by name: today `SubprocessExecutor`'s verdict source is the
    control sidecar, but the envelope may later come from an inline marker in an
    artifact (issue #3) — the failure ("runtime produced no result") is the same,
    only the concrete source named in the trailing parenthetical differs.

    Lead with the REAL cause (the runtime error the runner recognised, or the
    last output) — that is what a CLI user needs — then the runner's own
    diagnosis-safe command `display` (prompt already elided by the runner), its
    exit code, and cwd. The concrete mechanism that yielded nothing (the sidecar)
    is a trailing debug parenthetical.
    """
    if sup.errors:
        # De-duplicate, preserve order.
        seen: list[str] = []
        for e in sup.errors:
            if e not in seen:
                seen.append(e)
        lead = "; ".join(seen)
    elif sup.tail:
        lead = "runtime produced no result — last output: " + " | ".join(sup.tail[-3:])
    else:
        lead = "runtime produced no result and no diagnostic output"
    where = f" [cwd: {cwd}]" if cwd else ""
    return f"{lead}\n  command: {display}{where}\n  (exit={returncode}, no control sidecar written, completion={sup.completion})"


class SubprocessExecutor(AgentExecutor):
    """AgentExecutor that runs an agent as a supervised OS subprocess.

    This is the execution model the library shipped with: spawn a CLI runtime,
    supervise it by LIVENESS (kill only when stale), and read the agent's verdict
    from a control SIDECAR it writes. The two runtime-specific wire details (build
    the argv, parse a stdout line into an Event) are delegated to an `AgentRunner`
    — this executor's PRIVATE strategy, not the public seam.

    The control sidecar and the control preamble are this executor's OWN
    mechanism (an in-process executor has neither): the sidecar path is derived
    per node from run_dir + the node name, cleared before the run, and read back
    as the sole completion verdict.

    Supervision:
      - killed ONLY when STALE: no runner event AND no sidecar for
        `inv.idle_timeout_s`. An actively-emitting agent runs as long as it makes
        progress — no absolute wall-clock cap.
      - completion detected the moment the sidecar appears (a done-but-lingering
        process is finished immediately).

    `env_extra` (constructor) adds process env vars for every run on this
    executor.
    """

    def __init__(self, runner: AgentRunner, *, env_extra: dict[str, str] | None = None) -> None:
        self.runner = runner
        self.name = getattr(runner, "name", "subprocess")
        self._env_extra = env_extra

    def run(self, inv: AgentInvocation, *, control_file: Path | None = None) -> AgentResult:
        """Run one invocation as a supervised subprocess -> AgentResult.

        `control_file` overrides the per-node sidecar path; by default it is
        `inv.run_dir / "<node-or-agent>.control.json"`. It is a subprocess-private
        argument (not part of the neutral AgentInvocation).

        Raises:
            AgentTimeoutError: the agent went stale (idle) with no valid sidecar.
            AgentContentFailedError: sidecar reports a non-ok status (do not retry).
        """
        agent = inv.agent
        run_dir = inv.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)

        # Per-node control sidecar (node name is unique per run; falls back to the
        # agent name). Derived here — the sidecar is this executor's mechanism.
        if control_file is None:
            base = inv.node or agent
            control_file = run_dir / f"{base}.control.json"
        # Defensive: clear a stale sidecar so completion keys only on THIS run's write.
        control_file.unlink(missing_ok=True)

        # Compose the final prompt. Order:
        #   [control preamble] [compose_prompt: run-wide context+instructions + prompt]
        # The preamble is subprocess-specific (it tells the agent to write the
        # sidecar) so it is prepended HERE; everything else comes from the neutral
        # compose_prompt helper. A result schema, if supplied, is embedded in the
        # preamble block.
        schema = coerce_schema(inv.result_schema)
        schema_dict = schema.to_json_schema() if schema is not None else None
        preamble = build_control_preamble(agent, str(control_file), schema_dict)
        full_prompt = preamble + "\n\n" + compose_prompt(inv)

        agent_dir = inv.agent_dir or ""
        spec = self.runner.build_command(replace(inv, prompt=full_prompt))

        env = os.environ.copy()
        if self._env_extra:
            env.update(self._env_extra)

        # cwd = agent_dir when set (the runtime's project dir, e.g. where opencode
        # finds .opencode/agent). Not run_dir — that is the artifact/sidecar root.
        start = time.monotonic()
        try:
            proc = subprocess.Popen(
                spec.argv,
                cwd=agent_dir or None,
                env=env,
                stdout=subprocess.PIPE,
                # capture_stderr=True: separate pipe so the runner's
                # parse_stderr_line can extract actionable error detail without
                # polluting the stdout JSON parse. False: merge into stdout
                # (backward-compatible default for runners that don't opt in).
                stderr=subprocess.PIPE if spec.capture_stderr else subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        except OSError as exc:
            # The runtime binary is missing / not executable / cwd is invalid.
            # Transient-in-principle (fix PATH and retry), so AgentCrashError.
            raise AgentCrashError(f"agent {agent!r}: failed to start ({spec.display}): {exc}") from exc

        stderr_buf = _start_stderr_reader(proc) if spec.capture_stderr else None
        sup = _supervise(
            proc, runner=self.runner, idle_timeout_s=inv.idle_timeout_s, control_file=control_file, on_event=inv.on_event, stderr_buf=stderr_buf
        )
        duration = time.monotonic() - start

        # The control sidecar is the SOLE verdict. No sidecar => error; the engine
        # never inspects artifacts to guess success (that is the gate's job).
        sidecar = _read_sidecar(control_file)
        if sidecar is None:
            sidecar = {"status": "error", "agent": agent, "reason": _no_verdict_reason(sup, proc.returncode, spec.display, agent_dir)}

        result = self.assemble_result(
            inv,
            sidecar,
            exit_code=proc.returncode,
            duration_s=duration,
            tokens=sup.tokens,
            cost=sup.cost,
            events=sup.events,
            completion=sup.completion,
            runtime=self.name,  # the runner's name, e.g. "opencode"
        )

        # Subprocess-only failure routing (only when there is no usable verdict):
        if sidecar.get("status") not in ("ok", "verified"):
            # 1. Stale (idle timeout, hung) -> transient; retry may help.
            if sup.completion == "stale":
                raise AgentTimeoutError(
                    f"agent {agent!r} went stale after {duration:.1f}s (no event/sidecar for {inv.idle_timeout_s}s)"
                    + (f" — last output: {sup.tail[-1]}" if sup.tail else "")
                )
            # 2. The runtime reported a startup/config error (e.g. model
            #    unresolved) — DETERMINISTIC, so do NOT retry: a content failure.
            #    (check_content_status below raises AgentContentFailedError.)
            # 3. No error event but the process exited NON-ZERO with no sidecar ->
            #    a genuine crash (OOM, killed, transient provider 5xx) -> retry.
            if not sup.errors and proc.returncode not in (0, None):
                raise AgentCrashError(f"agent {agent!r}: {sidecar['reason']}")

        # Shared content-status policy — same check MockExecutor calls. Raises
        # AgentContentFailedError (do-not-retry) for a runtime error or a clean
        # exit with no sidecar.
        self.check_content_status(agent, sidecar)
        return result


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
    node: str = "",
) -> AgentResult:
    """Run one agent as a supervised subprocess (backward-compatible shim).

    This is a thin wrapper that builds a neutral `AgentInvocation` from the
    keyword arguments and delegates to `SubprocessExecutor`. It preserves the
    long-standing keyword API (callers and tests use it directly); new code
    should prefer building an `AgentInvocation` and calling an `AgentExecutor`.

    See `SubprocessExecutor` for the supervision/sidecar semantics.
    """
    inv = AgentInvocation(
        agent=agent,
        prompt=prompt,
        run_dir=run_dir,
        node=node,
        result_schema=result_schema,
        model=model,
        agent_dir=str(agent_dir) if agent_dir else "",
        instructions=instructions,
        shared_instructions=shared_instructions,
        shared_context=shared_context,
        idle_timeout_s=idle_timeout_s,
        on_event=on_event,
    )
    return SubprocessExecutor(runner, env_extra=env_extra).run(inv, control_file=control_file)


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
