"""LIVENESS supervision of a spawned agent process — the anyio machinery.

Reader tasks stream the child's stdout (and, when opted in, stderr) through a
decode + line-framing layer into anyio memory object streams; the main coroutine
folds each stdout line through `runner.parse_event`, and each real event resets
an idle deadline.

The agent is killed ONLY when STALE: no event AND no sidecar for
`idle_timeout_s`. An actively-emitting agent runs as long as it makes progress —
there is no absolute cap. Completion is detected the moment the sidecar appears
on disk, so a done-but-lingering process is finished immediately.

Cancellation (Ctrl-C, a cancelled task group) ALWAYS reaps the whole process
group through a SHIELDED cancel scope, so an opencode and its MCP children are
never orphaned.

Separate from `subprocess_exec.py` so the executor reads as policy — build the
command, supervise, read the verdict — while the process mechanics live here.
"""

from __future__ import annotations

import codecs
import json
import math
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import anyio
from anyio.abc import ByteReceiveStream, Process
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from loguru import logger

from agent_flow.runners.base import AgentRunner
from agent_flow.runners.events import Event

_TAIL_LINES = 20


# Longest silent poll window when the stream is quiet, so the sidecar `.exists()`
# probe still fires even if the process emits nothing (matches the old 1.0s cap).
_POLL_CAP_S = 1.0


# Two DIFFERENT waits, deliberately separate constants.
#
# 1. How long to let a finished-but-still-talking AGENT complete its turn.
#
# The control sidecar appears when the agent's `write` tool RUNS — several
# seconds before its turn actually ends. It still has to return the tool result,
# close the step (a final model roundtrip), flush telemetry/session state and
# shut its MCP children down. So the sidecar means "the verdict is safe to read",
# NOT "the process is done". Supervision keeps consuming events for this long
# after the sidecar lands, so the agent can finish and exit on its own; only an
# agent that outstays it is stopped. An UPPER BOUND, not a fixed wait — a clean
# turn ends the window early (terminal event or EOF), costing only its real time.
#
# Deliberately generous. Observed close times scale with the size of the turn:
# ~5s for a 21-event readiness check, ~9s at 39 events, ~11s at 303 events and
# 13M tokens. 30s is ~3x the largest measured close, and the headroom is close to
# free: it is spent ONLY by an agent that writes its verdict and then never closes
# its turn at all — never observed — and even then costs one bounded wait on one
# node. Erring long is the cheap direction; erring short kills healthy agents
# mid-turn, which is the bug this window exists to fix.
_FINISH_GRACE_S = 30

# 2. How long to wait for a PROCESS to die: before signalling it at all, and
#    again between SIGTERM and SIGKILL. Measured: a genuinely finished opencode
#    exits in ~0.1s (with or without MCP attached), and a SIGTERM'd one in ~20ms,
#    so this only ever elapses for a process that is truly stuck.
_KILL_GRACE_S = 5

# How often to report that we are still waiting on a SILENT agent. Without this
# a stalled long-duration node looks identical to a hung ORCHESTRATOR for up to
# ten minutes: no output at all, nothing to tell the operator whether anything is
# still alive or which agent is holding the run up. Each beat names the agent and
# how long it has been quiet, so the silence is visible as it accumulates rather
# than only in hindsight from the kill line. Emitted at INFO (the level an
# operator watches), and only while the agent is actually quiet — an agent
# emitting events needs no beat, since its events already are the heartbeat.
_HEARTBEAT_S = 30


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


# --- async line framing (anyio open_process yields BYTES, not text lines) ----


async def _iter_lines(stream: ByteReceiveStream) -> AsyncIterator[str]:
    """Yield decoded stdout/stderr LINES from a raw anyio byte stream.

    anyio's `open_process` gives BYTE streams with no `text=True` and no line
    iteration — so we reimplement universal-newline-ish line framing: decode
    utf-8 (replacing undecodable bytes so a stray byte never crashes the run),
    accumulate a buffer, and split on "\n". A trailing partial line (no final
    newline) is yielded at EOF, mirroring `for line in proc.stdout`. Each yielded
    line keeps its trailing "\n" (except a final unterminated one), so downstream
    tail/rstrip logic is unchanged from the threaded reader.

    Decoding is INCREMENTAL: a multi-byte UTF-8 character split across two chunk
    boundaries is held until its remaining bytes arrive (a per-chunk
    `bytes.decode` would corrupt it into two replacement chars). Genuinely
    invalid bytes still degrade to U+FFFD rather than raising.
    """
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buf = ""
    async for chunk in stream:
        buf += decoder.decode(chunk)
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line + "\n"
    buf += decoder.decode(b"", final=True)  # flush any dangling partial sequence
    if buf:
        yield buf


async def _pump_lines(stream: ByteReceiveStream | None, send: MemoryObjectSendStream[str | None]) -> None:
    """Reader task: push each decoded line onto a memory stream; None marks EOF.

    Mirrors the old daemon-thread reader (`_start_line_reader`): isolate the
    (blocking-in-spirit) read from the supervision loop so the loop can enforce
    the idle deadline even when the process is silent. Sending a trailing None is
    the EOF sentinel the loop keys on. The send stream is closed on the way out
    so a consumer that stops early does not wedge the reader.
    """
    try:
        if stream is not None:
            async for line in _iter_lines(stream):
                await send.send(line)
        logger.debug("reader: stream EOF")
    finally:
        # Best-effort EOF sentinel, then close. If the consumer already closed
        # the receive end (early stop), the send raises — swallow it.
        try:
            await send.send(None)
        except anyio.BrokenResourceError:
            pass
        send.close()


def _apply_event(line: str, st: dict, runner: AgentRunner, on_event: Callable[[Event], None] | None) -> bool:
    """Fold one stdout line into supervision state via the runner's parser.

    Returns True if the line was a real event (a liveness heartbeat), False if
    it was noise (not counted toward liveness). If on_event is set, it is called
    with each real event for optional live display — guarded so a display error
    never disrupts supervision.

    Parsing itself is guarded for the same reason: `parse_event` is a PUBLIC seam
    (any runner may implement it) and a runtime can always emit an unexpected
    shape. A parser that raises must degrade that ONE line to noise, never abort
    an otherwise healthy run — the line is still kept in the diagnostic tail by
    `_consume_line`.
    """
    try:
        ev = runner.parse_event(line)
    except Exception as exc:  # noqa: BLE001 - a parser bug/odd shape must not kill the run
        logger.debug(f"parse_event failed on a line ({type(exc).__name__}: {exc}) — treating it as noise")
        return False
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


def _finish(st: dict, kind: str) -> _Supervision:
    """Build the _Supervision result (killing, if needed, is the caller's job)."""
    return _Supervision(
        completion=kind,
        tokens=st["tokens"],
        cost=st["cost"],
        events=st["events"],
        errors=tuple(st["errors"]),
        tail=tuple(st["tail"]),
    )


def _drain_stderr(stderr_rx: MemoryObjectReceiveStream[str | None] | None, runner: AgentRunner, errors: list) -> None:
    """Non-blocking drain of any stderr lines already queued.

    Called at the end of the supervision loop (after EOF or kill) to flush
    whatever the stderr reader task has buffered. Each line is passed to
    runner.parse_stderr_line (if implemented); non-None results are appended to
    `errors` so _no_verdict_reason can include the real cause.

    Non-blocking: only drains what is already in the memory stream — never
    waits. The reader task pushes all lines before EOF, so once the process has
    exited (EOF/kill) the buffered lines are complete.
    """
    if stderr_rx is None:
        return
    parse_stderr = getattr(runner, "parse_stderr_line", None)
    if parse_stderr is None:
        return
    while True:
        try:
            line = stderr_rx.receive_nowait()
        except anyio.WouldBlock, anyio.EndOfStream:
            break
        if line is None:
            break
        msg = parse_stderr(line)
        if msg:
            errors.append(msg)


async def _supervise(
    proc: Process,
    *,
    runner: AgentRunner,
    idle_timeout_s: float,
    control_file: Path | None,
    on_event: Callable[[Event], None] | None = None,
    capture_stderr: bool = False,
    agent: str = "",
) -> _Supervision:
    """Supervise a running agent by LIVENESS only — kill solely when idle.

    Reader tasks stream stdout (+ optional stderr) into memory streams; the main
    loop folds each stdout line into an Event. The loop:
      - resets the IDLE deadline on every real event (the heartbeat),
      - stops early ("sidecar") the moment the control sidecar appears on disk
        — the agent's work is done even if the process lingers,
      - stops ("completed") when the stdout reader hits EOF (process finished),
      - kills only on STALE: no event AND no sidecar for idle_timeout_s,
      - reports every `_HEARTBEAT_S` while the agent is SILENT, naming it and the
        budget left, so a long quiet stretch is visible as it happens instead of
        looking like a hung orchestrator.

    Runner-agnostic: the only runner-specific step is `runner.parse_event`.
    On cancellation (Ctrl-C / a cancelled parent task group) the process group is
    reaped via a shielded kill before the cancellation propagates, so no orphaned
    opencode + MCP children survive.
    """
    # Unbounded reader buffers (like the previous thread + queue.Queue): a reader
    # task must NEVER block on send, or it stops draining the OS pipe and the child
    # blocks writing -> deadlock. stderr especially is only drained at the end
    # (_drain_stderr), so a bounded buffer could wedge a chatty child.
    stdout_tx, stdout_rx = anyio.create_memory_object_stream[str | None](max_buffer_size=math.inf)
    stderr_tx, stderr_rx = anyio.create_memory_object_stream[str | None](max_buffer_size=math.inf) if capture_stderr else (None, None)
    try:
        async with anyio.create_task_group() as tg:
            tg.start_soon(_pump_lines, proc.stdout, stdout_tx)
            if stderr_tx is not None:
                tg.start_soon(_pump_lines, proc.stderr, stderr_tx)
            sup = await _supervise_loop(
                proc,
                runner=runner,
                idle_timeout_s=idle_timeout_s,
                control_file=control_file,
                on_event=on_event,
                stdout_rx=stdout_rx,
                stderr_rx=stderr_rx,
                agent=agent,
            )
            tg.cancel_scope.cancel()  # tear down any still-running reader
        return sup
    except BaseException:
        # KeyboardInterrupt OR cancellation (anyio's cancel exc is a
        # BaseException): reap the whole process group before propagating, so we
        # never leave an orphaned opencode (and its MCP children) running.
        await _kill_group(proc)
        raise


async def _supervise_loop(
    proc: Process,
    *,
    runner: AgentRunner,
    idle_timeout_s: float,
    control_file: Path | None,
    on_event: Callable[[Event], None] | None,
    stdout_rx: MemoryObjectReceiveStream[str | None],
    stderr_rx: MemoryObjectReceiveStream[str | None] | None,
    agent: str = "",
) -> _Supervision:
    """The liveness loop (see _supervise). Extracted so _supervise stays a thin
    spawn/interrupt wrapper (and both stay under the complexity limit).

    Completion has three shapes, and only the first is a failure:

      - STALE — silent for the whole idle window with no verdict. Genuinely hung,
        so its group is killed immediately (shielded `_kill_group`).
      - FINISHED — the agent emitted its terminal event (or the stream hit EOF)
        with its verdict on disk. The clean path: the process is already exiting,
        so it is simply reaped.
      - FINISH WINDOW ELAPSED — the verdict landed but the turn never closed
        within `_FINISH_GRACE_S`; stop it.

    Note the sidecar appearing is NOT by itself a stop: it means the verdict is
    safe to read, while the agent is typically still closing its step. Treating it
    as completion is what used to SIGTERM every agent mid-turn.
    """
    st: dict = {"tokens": 0, "cost": 0.0, "events": 0, "saw_terminal": False, "errors": [], "tail": deque(maxlen=_TAIL_LINES)}
    started = anyio.current_time()
    idle_deadline = started + idle_timeout_s
    # Heartbeat bookkeeping: when the agent last spoke, and when to next report
    # that it has not. Both move together on every real event.
    quiet_since, next_beat = started, started + _HEARTBEAT_S
    sidecar_present = _sidecar_probe(control_file)
    # Set when the sidecar first appears: from then on the agent is FINISHING
    # (verdict written, turn not yet closed) and this bounds how long we let it.
    finish_deadline: float | None = None

    while True:
        now = anyio.current_time()
        if finish_deadline is None and sidecar_present():
            # The verdict is on disk, but the agent is still mid-turn. Do NOT stop
            # here — keep consuming its events so it can close the step, flush and
            # exit on its own. The clean end comes from its terminal event or EOF
            # below; this deadline is only the backstop.
            finish_deadline = now + _FINISH_GRACE_S
            logger.debug(f"supervise: sidecar on disk (events={st['events']}) -> letting the agent finish (<= {_FINISH_GRACE_S}s)")

        if finish_deadline is not None:
            if now >= finish_deadline:
                # Wrote its verdict but never closed the turn — stop it.
                logger.debug(f"supervise: finish window elapsed (events={st['events']}) -> stopping")
                await _stop_process(proc)
                _drain_stderr(stderr_rx, runner, st["errors"])
                return _finish(st, "sidecar")
        elif now >= idle_deadline:
            # Silent for the whole idle window with no verdict: genuinely hung.
            # No clean self-exit is coming, so kill its group now.
            logger.debug(f"supervise: STALE — no event/sidecar for {idle_timeout_s}s (events={st['events']}) -> killing")
            await _kill_group(proc)
            _drain_stderr(stderr_rx, runner, st["errors"])
            return _finish(st, "stale")

        # Wait for the next line, but never longer than the poll cap, so the
        # sidecar/deadline checks above keep firing even during silence. A fresh
        # window each iteration = "deadline resets on activity". While finishing,
        # bound by THAT deadline instead — the idle one no longer applies (the
        # agent has already delivered its verdict).
        active_deadline = finish_deadline if finish_deadline is not None else idle_deadline
        wait = max(min(active_deadline - anyio.current_time(), _POLL_CAP_S), 0.05)
        line: str | None = None
        got_line = False
        with anyio.move_on_after(wait):
            line = await stdout_rx.receive()
            got_line = True
        if not got_line:
            # Timed out with no line — re-check stop conditions, and tell the
            # operator we are still here and what we are waiting for.
            next_beat = _beat(agent, quiet_since, active_deadline, next_beat, finishing=finish_deadline is not None)
            continue

        if line is None:  # reader EOF — process finished
            logger.debug(f"supervise: stdout EOF (events={st['events']}) -> reaping")
            await _stop_process(proc)
            _drain_stderr(stderr_rx, runner, st["errors"])
            return _finish(st, "completed")

        # Line arrived: fold it in (tail + events + errors). A real event is a
        # heartbeat -> reset the idle deadline. A terminal event once the sidecar
        # is on disk stops promptly (else the top-of-loop check catches it next).
        if _consume_line(line, st, runner, on_event):
            now = anyio.current_time()
            idle_deadline = now + idle_timeout_s
            # The agent spoke: its event IS the heartbeat, so restart both the
            # silence clock and the next beat.
            quiet_since, next_beat = now, now + _HEARTBEAT_S
        if st["saw_terminal"] and sidecar_present():
            # The agent said it is done (its terminal event) AND wrote its verdict:
            # the cleanest completion signal there is — give it the grace window to
            # exit on its own before escalating to a kill.
            logger.debug(f"supervise: terminal event + sidecar (events={st['events']}) -> stopping")
            await _stop_process(proc)
            _drain_stderr(stderr_rx, runner, st["errors"])
            return _finish(st, "sidecar")


def _beat(agent: str, quiet_since: float, deadline: float, next_beat: float, *, finishing: bool) -> float:
    """Report that a SILENT agent is still being waited on; return the next due time.

    A no-op until `next_beat` is reached, so the caller can invoke it on every
    idle poll (once per second) and get a line only every `_HEARTBEAT_S`.

    Names the agent because a parallel group has several in flight and the
    operator needs to know WHICH one is holding the run up, and reports both the
    silence so far and the budget left — the two numbers that say whether to keep
    waiting or go look at the agent. `finishing` distinguishes the two silences:
    waiting for an agent to WORK, versus waiting for one that has already written
    its verdict to close its turn.
    """
    now = anyio.current_time()
    if now < next_beat:
        return next_beat
    what = "closing its turn" if finishing else "working"
    who = f"agent {agent!r}" if agent else "agent"
    logger.info(f"still waiting on {who} ({what}): silent for {now - quiet_since:.0f}s, {max(deadline - now, 0):.0f}s left")
    return now + _HEARTBEAT_S


async def _stop_process(proc: Process) -> None:
    """Wind a FINISHED agent down gracefully: give it a grace window to exit on
    its own, and only kill its group if it overstays.

    By the time this is called the agent has CLOSED its turn (terminal event or
    EOF), or has overrun `_FINISH_GRACE_S` — so on the happy path the process is
    already exiting and `proc.wait()` returns in milliseconds. We still wait up to
    `_KILL_GRACE_S` rather than signalling blind, and only escalate via
    `_kill_group` (SIGTERM -> grace -> SIGKILL) if it truly overstays.

    This is the runtime-AGNOSTIC teardown policy; deciding WHAT counts as a
    completion/terminal signal is the runner's job (parse_event -> saw_terminal).
    Also used to reap an EOF-exited process (already gone -> proc.wait returns at
    once, no kill).
    """
    with anyio.move_on_after(_KILL_GRACE_S) as scope:
        await proc.wait()
    if scope.cancelled_caught:
        await _kill_group(proc)


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


async def _kill_group(proc: Process) -> None:
    """Terminate the child's whole process group: SIGTERM, grace, then SIGKILL.

    Runs inside a SHIELDED cancel scope so the kill completes even when the
    caller is being cancelled (Ctrl-C, a cancelled parent task group) — this is
    the invariant that guarantees no orphaned opencode + MCP children.
    `os.killpg` stays (a non-blocking syscall); only the between-signal waits are
    async. After exhausting both signals it falls back to a direct `proc.kill()`
    so we never leave a zombie regardless of whether os.killpg reached the leader.
    """
    with anyio.CancelScope(shield=True):
        try:
            pgid = os.getpgid(proc.pid)
        except ProcessLookupError:
            return  # process already gone

        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(pgid, sig)
                logger.debug(f"kill_group: sent {sig!s} to pgid={pgid} (pid={proc.pid})")
            except ProcessLookupError:
                return  # group already gone — nothing to do
            with anyio.move_on_after(_KILL_GRACE_S) as scope:
                await proc.wait()
            if not scope.cancelled_caught:
                return  # process exited after the signal

        # Last resort: direct kill on the leader in case os.killpg missed it.
        try:
            proc.kill()
        except ProcessLookupError:
            return
        with anyio.move_on_after(_KILL_GRACE_S):
            await proc.wait()
