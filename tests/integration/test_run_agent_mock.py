"""Integration tests for run_agent via the domain-free subprocess stub.

These spawn the stub (_mock_agent.py) as a real subprocess and exercise the full
SubprocessExecutor supervision path (liveness loop, sidecar reading, kill,
classification) without any LLM or domain knowledge. The stub writes exactly the
envelope it is told to (StubRunner emit=...) or hangs (sleep=True). They are
integration tests because they cross a process boundary.
"""

import json

import pytest

from agent_flow.core.agent_runtime import SubprocessExecutor, run_agent
from agent_flow.runners.executor import AgentContentFailedError, AgentCrashError, AgentTimeoutError

# stub_runner is a fixture (tests/conftest.py) — not a plain import — so it
# resolves identically regardless of pytest invocation style (tests/ has no
# __init__.py, so a package-style import is import-mode-dependent).


def _prompt(report, control):
    return f"PRODUCT_KEY: test\nREPORT: {report}\nCONTROL_FILE: {control}"


def test_run_agent_ok(tmp_path, stub_runner):
    control = tmp_path / "n.control.json"
    result = run_agent(
        agent="a",
        prompt=_prompt(tmp_path / "out.md", control),
        run_dir=tmp_path,
        runner=stub_runner(emit={"status": "ok", "result": {"summary": "s", "languages": ["Python"]}}),
        control_file=control,
    )
    assert result.control["status"] == "ok"
    assert control.exists()


def test_run_agent_content_failure_not_retryable(tmp_path, stub_runner):
    # The stub emits a status:error SIDECAR -> SubprocessExecutor raises (no retry).
    control = tmp_path / "n.control.json"
    with pytest.raises(AgentContentFailedError):
        run_agent(
            agent="a",
            prompt=_prompt(tmp_path / "out.md", control),
            run_dir=tmp_path,
            runner=stub_runner(emit={"status": "error", "reason": "boom"}),
            control_file=control,
        )
    assert json.loads(control.read_text())["status"] == "error"


def test_run_agent_stale_timeout(tmp_path, stub_runner):
    # sleep=True makes the stub hang; with a tiny idle window and no sidecar,
    # supervision must declare it stale and kill it.
    control = tmp_path / "n.control.json"
    with pytest.raises(AgentTimeoutError):
        run_agent(
            agent="a",
            prompt=_prompt(tmp_path / "out.md", control),
            run_dir=tmp_path,
            runner=stub_runner(sleep=True),
            idle_timeout_s=2,
            control_file=control,
        )


def test_run_agent_sidecar_is_authoritative(tmp_path, stub_runner):
    # A pre-existing (stale) sidecar must be cleared before the run, so the
    # result reflects THIS run's sidecar, not the old one.
    control = tmp_path / "n.control.json"
    control.write_text(json.dumps({"status": "error", "reason": "stale"}))
    result = run_agent(
        agent="a",
        prompt=_prompt(tmp_path / "out.md", control),
        run_dir=tmp_path,
        runner=stub_runner(emit={"status": "ok"}),
        control_file=control,
    )
    assert result.control["status"] == "ok"  # fresh sidecar, not the stale error


def test_run_agent_validates_result_schema_valid(tmp_path, stub_runner):
    # A matching schema validates and attaches the outcome (engine never fails on it).
    control = tmp_path / "n.control.json"
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "languages": {"type": "array"}},
        "required": ["summary", "languages"],
    }
    result = run_agent(
        agent="a",
        prompt=_prompt(tmp_path / "out.md", control),
        run_dir=tmp_path,
        runner=stub_runner(emit={"status": "ok", "result": {"summary": "s", "languages": ["Python"]}}),
        control_file=control,
        result_schema=schema,
    )
    assert result.control["status"] == "ok"
    assert result.result_valid is True
    assert result.result_errors == ()


def test_run_agent_flags_invalid_result_without_failing(tmp_path, stub_runner):
    # A schema the emitted result does NOT satisfy: run still succeeds (status ok),
    # but result_valid is False so a gate can decide what to do.
    control = tmp_path / "n.control.json"
    schema = {"type": "object", "properties": {"nope": {"type": "string"}}, "required": ["nope"]}
    result = run_agent(
        agent="a",
        prompt=_prompt(tmp_path / "out.md", control),
        run_dir=tmp_path,
        runner=stub_runner(emit={"status": "ok", "result": {"summary": "s"}}),
        control_file=control,
        result_schema=schema,
    )
    assert result.control["status"] == "ok"  # engine did NOT fail the run
    assert result.result_valid is False


# --- no-verdict routing (no sidecar): crash vs. content-failure -------------


def test_no_sidecar_nonzero_exit_is_crash(tmp_path, stub_runner):
    # Process exits non-zero, prints a non-JSON line, writes NO sidecar and the
    # runner recognises no runtime error -> a genuine crash -> AgentCrashError
    # (transient, retryable).
    control = tmp_path / "n.control.json"
    with pytest.raises(AgentCrashError) as ei:
        run_agent(
            agent="a",
            prompt=_prompt(tmp_path / "out.md", control),
            run_dir=tmp_path,
            runner=stub_runner(print_line="boom on stderr-ish", exit_code=1),
            control_file=control,
        )
    # The diagnostic carries the exit code and the last output.
    assert "exit=1" in str(ei.value)
    assert "boom on stderr-ish" in str(ei.value)


def test_no_sidecar_clean_exit_is_content_failure(tmp_path, stub_runner):
    # Process exits 0 but writes NO sidecar (agent finished yet produced no
    # verdict) -> content failure -> AgentContentFailedError (do NOT retry).
    control = tmp_path / "n.control.json"
    with pytest.raises(AgentContentFailedError) as ei:
        run_agent(
            agent="a",
            prompt=_prompt(tmp_path / "out.md", control),
            run_dir=tmp_path,
            runner=stub_runner(print_line="did some work then forgot the sidecar", exit_code=0),
            control_file=control,
        )
    assert "no control sidecar written" in str(ei.value)


@pytest.mark.anyio
async def test_start_failure_binary_missing_is_crash(tmp_path):
    # A runner whose binary does not exist -> open_process raises OSError ->
    # AgentCrashError. Exercises the async executor directly (not the sync shim).
    from agent_flow.runners.base import AgentInvocation, LaunchSpec

    class _MissingBinaryRunner:
        name = "missing"

        def build_command(self, inv):
            return LaunchSpec(argv=["definitely-not-a-real-binary-xyz", "arg"], display="definitely-not-a-real-binary-xyz <prompt>")

        def parse_event(self, line):
            from agent_flow.runners.base import Event

            return Event.none()

    control = tmp_path / "n.control.json"
    inv = AgentInvocation(agent="a", prompt="p", run_dir=tmp_path, node="n")
    with pytest.raises(AgentCrashError) as ei:
        await SubprocessExecutor(_MissingBinaryRunner()).run(inv, control_file=control)
    assert "failed to start" in str(ei.value)


@pytest.mark.anyio
async def test_raising_parse_event_degrades_to_noise_and_run_survives(tmp_path):
    """A runner whose `parse_event` RAISES must not abort an otherwise fine run.

    `AgentRunner` is a public seam and a runtime can always emit an unexpected
    shape, so a parser failure degrades that one line to noise (the line is still
    kept in the diagnostic tail); the sidecar verdict still decides the outcome.
    """
    from agent_flow.runners.base import AgentInvocation, LaunchSpec

    control = tmp_path / "n.control.json"
    prog = f"import json,sys; print('noise'); sys.stdout.flush(); open({str(control)!r},'w').write(json.dumps({{'status':'ok','agent':'a'}}))"

    class _RaisingParserRunner:
        name = "raising"

        def build_command(self, inv):  # noqa: ARG002
            return LaunchSpec(argv=["python3", "-c", prog], display="python3 -c <writes sidecar>")

        def parse_event(self, line):  # noqa: ARG002
            raise RuntimeError("parser blew up")

    inv = AgentInvocation(agent="a", prompt="p", run_dir=tmp_path, node="n", idle_timeout_s=10)
    result = await SubprocessExecutor(_RaisingParserRunner()).run(inv, control_file=control)
    assert result.control["status"] == "ok"


@pytest.mark.anyio
async def test_stdin_is_closed_agent_reading_stdin_does_not_hang(tmp_path):
    """Regression: the spawned agent must get a CLOSED stdin (immediate EOF).

    anyio's `open_process` defaults stdin to an OPEN pipe (unlike subprocess.Popen,
    which inherits the parent's stdin), so an agent that reads stdin — as opencode
    does — blocks forever waiting for input that never comes. The executor must
    pass stdin=DEVNULL. This runner's command reads stdin FIRST (which returns ""
    on EOF), then writes its ok sidecar; if stdin were an open pipe it would hang
    until the idle timeout. A short idle window keeps the test fast if it regresses.
    """
    from agent_flow.runners.base import AgentInvocation, Event, LaunchSpec

    control = tmp_path / "n.control.json"
    # read all of stdin (EOF -> ""), then write the sidecar and exit 0.
    prog = f"import sys,json; sys.stdin.read(); open({str(control)!r},'w').write(json.dumps({{'status':'ok','agent':'a'}}))"

    class _StdinReadingRunner:
        name = "stdin-reader"

        def build_command(self, inv):  # noqa: ARG002
            return LaunchSpec(argv=["python3", "-c", prog], display="python3 -c <reads stdin>")

        def parse_event(self, line):  # noqa: ARG002
            return Event.none()

    inv = AgentInvocation(agent="a", prompt="p", run_dir=tmp_path, node="n", idle_timeout_s=10)
    result = await SubprocessExecutor(_StdinReadingRunner()).run(inv, control_file=control)
    assert result.control["status"] == "ok"  # completed promptly — stdin got EOF, no hang
