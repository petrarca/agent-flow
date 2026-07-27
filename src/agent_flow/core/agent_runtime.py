"""Supervised agent runner — the runner-agnostic core of the library.

Spawns an agent as an OS subprocess via an `AgentRunner` (opencode, Claude Code,
mock, …), supervises it by LIVENESS (not wall-clock), and reads its result from a
per-agent STATUS SIDECAR written by the agent itself.

Supervision model (async, anyio):
  - Reader tasks stream the runner's stdout (and, when opted in, stderr) through
    a decode + line-framing layer into anyio memory object streams; the main
    coroutine folds each stdout line through `runner.parse_event`, and each real
    event resets an idle deadline.
  - The agent is killed ONLY when STALE: no event AND no sidecar for
    `idle_timeout_s`. An actively-emitting agent runs as long as it makes
    progress — there is no absolute cap.
  - Completion is detected the moment the sidecar appears on disk, so a
    done-but-lingering process is finished immediately.
  - Cancellation (Ctrl-C, a cancelled task group) ALWAYS reaps the whole process
    group via a SHIELDED kill, so we never orphan an opencode + its MCP children.

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
import math
import os
import signal
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, replace
from pathlib import Path
from subprocess import DEVNULL, PIPE, STDOUT

import anyio
from anyio.abc import ByteReceiveStream, Process
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream
from loguru import logger

from agent_flow.core.control_protocol import build_control_preamble
from agent_flow.core.schema import ResultSchema, coerce_schema
from agent_flow.runners import AgentInvocation, AgentRunner, Event
from agent_flow.runners.base import DEFAULT_IDLE_TIMEOUT_S, compose_prompt
from agent_flow.runners.executor import AgentCrashError, AgentExecutor, AgentResult, AgentTimeoutError

# How many trailing raw stdout lines to keep for a no-sidecar diagnostic.
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
    """
    buf = ""
    async for chunk in stream:
        buf += chunk.decode("utf-8", errors="replace")
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line + "\n"
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

    async def run(self, inv: AgentInvocation, *, control_file: Path | None = None) -> AgentResult:
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

        control_file = self._resolve_control_file(inv, control_file)
        full_prompt = self._compose_full_prompt(inv, control_file)

        agent_dir = inv.agent_dir or ""
        spec = self.runner.build_command(replace(inv, prompt=full_prompt))

        env = os.environ.copy()
        if self._env_extra:
            env.update(self._env_extra)

        # cwd = agent_dir when set (the runtime's project dir, e.g. where opencode
        # finds .opencode/agent). Not run_dir — that is the artifact/sidecar root.
        start = anyio.current_time()
        try:
            proc = await anyio.open_process(
                spec.argv,
                cwd=agent_dir or None,
                env=env,
                # No stdin: agents are non-interactive and never read it. anyio's
                # open_process defaults stdin to a PIPE (unlike subprocess.Popen,
                # which inherits the parent's stdin) — an OPEN, unwritten stdin
                # pipe makes opencode block forever waiting for input. DEVNULL
                # gives an immediate EOF, matching the old Popen(stdin inherited /
                # closed) behaviour. This was the async-migration hang.
                stdin=DEVNULL,
                stdout=PIPE,
                # capture_stderr=True: separate pipe so the runner's
                # parse_stderr_line can extract actionable error detail without
                # polluting the stdout JSON parse. False: merge into stdout
                # (backward-compatible default for runners that don't opt in).
                stderr=PIPE if spec.capture_stderr else STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            # The runtime binary is missing / not executable / cwd is invalid.
            # Transient-in-principle (fix PATH and retry), so AgentCrashError.
            raise AgentCrashError(f"agent {agent!r}: failed to start ({spec.display}): {exc}") from exc

        logger.debug(
            f"spawned agent={agent!r} pid={proc.pid} cwd={agent_dir or os.getcwd()!r} "
            f"capture_stderr={spec.capture_stderr} idle_timeout_s={inv.idle_timeout_s} :: {spec.display}"
        )
        async with proc:
            sup = await _supervise(
                proc,
                runner=self.runner,
                idle_timeout_s=inv.idle_timeout_s,
                control_file=control_file,
                on_event=inv.on_event,
                capture_stderr=spec.capture_stderr,
            )
            returncode = proc.returncode
        duration = anyio.current_time() - start

        return self._finalize(inv, control_file, spec, sup, returncode, agent_dir, duration)

    def _resolve_control_file(self, inv: AgentInvocation, control_file: Path | None) -> Path:
        """Per-node control sidecar path (node name is unique per run; falls back
        to the agent name). Cleared defensively so completion keys only on THIS
        run's write. The sidecar is this executor's own mechanism."""
        if control_file is None:
            base = inv.node or inv.agent
            control_file = inv.run_dir / f"{base}.control.json"
        control_file.unlink(missing_ok=True)
        return control_file

    def _compose_full_prompt(self, inv: AgentInvocation, control_file: Path) -> str:
        """Compose the final prompt: [verdict preamble] + [compose_prompt].

        HOW the agent reports its verdict is the RUNNER's protocol
        (build_verdict_preamble). The runner returns the instruction block; the
        executor prepends it. A runner that does not implement it falls back to
        the shared sidecar preamble (build_control_preamble) — identical output
        for opencode, which simply delegates to that helper. A result schema, if
        supplied, is embedded in the preamble block."""
        schema = coerce_schema(inv.result_schema)
        schema_dict = schema.to_json_schema() if schema is not None else None
        build_preamble = getattr(self.runner, "build_verdict_preamble", None)
        if callable(build_preamble):
            preamble = build_preamble(inv.agent, str(control_file), schema_dict)
        else:
            preamble = build_control_preamble(inv.agent, str(control_file), schema_dict)
        return preamble + "\n\n" + compose_prompt(inv)

    def _finalize(
        self,
        inv: AgentInvocation,
        control_file: Path,
        spec,  # noqa: ANN001 - LaunchSpec (runner-private type)
        sup: _Supervision,
        returncode: int | None,
        agent_dir: str,
        duration: float,
    ) -> AgentResult:
        """Read the sidecar verdict, assemble the result, and route failures.

        The control sidecar is the SOLE verdict. No sidecar => error; the engine
        never inspects artifacts to guess success (that is the gate's job)."""
        agent = inv.agent
        sidecar = _read_sidecar(control_file)
        if sidecar is None:
            sidecar = {"status": "error", "agent": agent, "reason": _no_verdict_reason(sup, returncode, spec.display, agent_dir)}

        result = self.assemble_result(
            inv,
            sidecar,
            exit_code=returncode,
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
            if not sup.errors and returncode not in (0, None):
                raise AgentCrashError(f"agent {agent!r}: {sidecar['reason']}")

        # Shared content-status policy — same check MockExecutor calls. Raises
        # AgentContentFailedError (do-not-retry) for a runtime error or a clean
        # exit with no sidecar.
        self.check_content_status(agent, sidecar)
        return result


async def arun_agent(
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
    """Run one agent as a supervised subprocess (async — the native entry point).

    Builds a neutral `AgentInvocation` from the keyword arguments and delegates to
    `SubprocessExecutor`. New async code should call this (or build an
    `AgentInvocation` and call an `AgentExecutor` directly); the sync `run_agent`
    wrapper keeps the long-standing blocking API for existing callers/tests.

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
    return await SubprocessExecutor(runner, env_extra=env_extra).run(inv, control_file=control_file)


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
    """Run one agent as a supervised subprocess (sync back-compatible shim).

    A thin `anyio.run` wrapper over `arun_agent` — it preserves the long-standing
    blocking keyword API that Tier-1/2 callers, examples, and tests use directly.
    New async code should prefer `arun_agent` (no event-loop bridge) or building
    an `AgentInvocation` and calling an `AgentExecutor`.

    See `SubprocessExecutor` for the supervision/sidecar semantics.
    """
    return anyio.run(
        lambda: arun_agent(
            agent=agent,
            prompt=prompt,
            run_dir=run_dir,
            runner=runner,
            agent_dir=agent_dir,
            idle_timeout_s=idle_timeout_s,
            model=model,
            instructions=instructions,
            env_extra=env_extra,
            control_file=control_file,
            result_schema=result_schema,
            on_event=on_event,
            shared_instructions=shared_instructions,
            shared_context=shared_context,
            node=node,
        )
    )


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
