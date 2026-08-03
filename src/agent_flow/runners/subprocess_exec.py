"""`SubprocessExecutor` — run an agent as a supervised OS subprocess.

The execution model the library shipped with: spawn a CLI runtime through an
`AgentRunner`, supervise it by LIVENESS (not wall-clock), and read its verdict
from a control SIDECAR the agent writes itself.

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

The control sidecar is the SOLE verdict. If it is absent the run is an error;
the engine never inspects an agent's artifacts to guess success. Any
domain-specific check ("a report file was written") and any flow routing belong
to the orchestration layer's GATE, not here — this module supervises exactly one
subprocess and knows nothing about stages, artifacts or the DAG.

It lives beside its three sibling executors (`inprocess`, `mock_exec`, and the
planned `serve_executor`) and implements the `AgentExecutor` ABC defined in
`runners/executor.py`, so `get_executor` is a flat dispatch over one package.
The anyio process mechanics it delegates to live in `supervision.py`.
"""

from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path
from subprocess import DEVNULL, PIPE, STDOUT
from typing import TYPE_CHECKING

import anyio
from loguru import logger

from agent_flow.protocol import build_control_preamble, coerce_schema
from agent_flow.runners.base import AgentRunner
from agent_flow.runners.executor import AgentCrashError, AgentExecutor, AgentResult, AgentTimeoutError
from agent_flow.runners.invocation import AgentInvocation, compose_prompt
from agent_flow.runners.supervision import _no_verdict_reason, _read_sidecar, _supervise, _Supervision

if TYPE_CHECKING:
    from upath import UPath


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

    async def run(self, inv: AgentInvocation, *, control_file: Path | UPath | None = None) -> AgentResult:
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

    def _resolve_control_file(self, inv: AgentInvocation, control_file: Path | UPath | None) -> Path:
        """Per-node control sidecar path (node name is unique per run; falls back
        to the agent name). Cleared defensively so completion keys only on THIS
        run's write. The sidecar is this executor's own mechanism.

        A subprocess writes REAL disk, so the sidecar must be a real local path.
        A non-local run_dir (e.g. `memory://…`) cannot work here — the spawned
        process has no view of an in-memory filesystem — so it is rejected with an
        actionable error rather than silently degraded into a bogus local path
        (`Path("memory://x")` is the relative directory `memory:/x`). The
        in-memory filesystem is for the mock/in-process path, which never reaches
        this executor."""
        if control_file is None:
            base = inv.node or inv.agent
            control_file = inv.run_dir / f"{base}.control.json"
        if not isinstance(control_file, Path):
            raise ValueError(
                f"{self.name}: run_dir {str(inv.run_dir)!r} is not a local path, but a subprocess writes real files. "
                "An in-memory run_dir (memory://…) is only supported for mock runs (mock_agents=True); "
                "pass a local run_dir for a real run."
            )
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
            preamble = build_preamble(inv.agent, str(control_file), schema_dict, inv.rerun)
        else:
            preamble = build_control_preamble(inv.agent, str(control_file), schema_dict, inv.rerun)
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
