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

Separated from `subprocess_exec.py` so the executor reads as the policy — build
the command, supervise, read the verdict — and this module holds the process
mechanics it delegates to.
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

from agent_flow.runners.base import AgentRunner, Event

_TAIL_LINES = 20


# Longest silent poll window when the stream is quiet, so the sidecar `.exists()`
# probe still fires even if the process emits nothing (matches the old 1.0s cap).
_POLL_CAP_S = 1.0


# Grace period (seconds) for a signalled process to exit before escalating.
_KILL_GRACE_S = 5


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


def _stop_kind(idle: bool, sidecar: bool) -> str | None:
    """Map the two pre-line stop conditions to a completion kind (or None)."""
    if sidecar:
        return "sidecar"  # work done, even if the process lingers
    if idle:
        return "stale"  # silent for the whole idle window
    return None


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
) -> _Supervision:
    """Supervise a running agent by LIVENESS only — kill solely when idle.

    Reader tasks stream stdout (+ optional stderr) into memory streams; the main
    loop folds each stdout line into an Event. The loop:
      - resets the IDLE deadline on every real event (the heartbeat),
      - stops early ("sidecar") the moment the control sidecar appears on disk
        — the agent's work is done even if the process lingers,
      - stops ("completed") when the stdout reader hits EOF (process finished),
      - kills only on STALE: no event AND no sidecar for idle_timeout_s.

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
) -> _Supervision:
    """The liveness loop (see _supervise). Extracted so _supervise stays a thin
    spawn/interrupt wrapper (and both stay under the complexity limit).

    On a stale or sidecar-present stop the lingering process is killed with a
    shielded `_kill_group`; on EOF the process is reaped (already exiting).
    """
    st: dict = {"tokens": 0, "cost": 0.0, "events": 0, "saw_terminal": False, "errors": [], "tail": deque(maxlen=_TAIL_LINES)}
    idle_deadline = anyio.current_time() + idle_timeout_s
    sidecar_present = _sidecar_probe(control_file)

    while True:
        # Stop conditions checked before waiting for the next line.
        stop = _stop_kind(anyio.current_time() >= idle_deadline, sidecar_present())
        if stop is not None:
            if stop == "stale":
                logger.debug(f"supervise: STALE — no event/sidecar for {idle_timeout_s}s (events={st['events']}) -> killing")
            else:  # "sidecar"
                logger.debug(f"supervise: sidecar on disk (events={st['events']}) -> stopping")
            await _kill_group(proc)  # idempotent; ensures no lingering process
            _drain_stderr(stderr_rx, runner, st["errors"])
            return _finish(st, stop)

        # Wait for the next line, but never longer than the poll cap, so the
        # sidecar/deadline checks above keep firing even during silence. A fresh
        # window each iteration = "deadline resets on activity".
        wait = max(min(idle_deadline - anyio.current_time(), _POLL_CAP_S), 0.05)
        line: str | None = None
        got_line = False
        with anyio.move_on_after(wait):
            line = await stdout_rx.receive()
            got_line = True
        if not got_line:
            continue  # timed out with no line — re-check stop conditions

        if line is None:  # reader EOF — process finished
            logger.debug(f"supervise: stdout EOF (events={st['events']}) -> reaping")
            await _reap(proc)
            _drain_stderr(stderr_rx, runner, st["errors"])
            return _finish(st, "completed")

        # Line arrived: fold it in (tail + events + errors). A real event is a
        # heartbeat -> reset the idle deadline. A terminal event once the sidecar
        # is on disk stops promptly (else the top-of-loop check catches it next).
        if _consume_line(line, st, runner, on_event):
            idle_deadline = anyio.current_time() + idle_timeout_s
        if st["saw_terminal"] and sidecar_present():
            logger.debug(f"supervise: terminal event + sidecar (events={st['events']}) -> stopping")
            await _kill_group(proc)
            _drain_stderr(stderr_rx, runner, st["errors"])
            return _finish(st, "sidecar")


async def _reap(proc: Process) -> None:
    """Wait briefly for a finished process; kill its group if it lingers."""
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
