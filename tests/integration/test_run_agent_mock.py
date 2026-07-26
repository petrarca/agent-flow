"""Integration tests for run_agent via the domain-free subprocess stub.

These spawn the stub (_mock_agent.py) as a real subprocess and exercise the full
SubprocessExecutor supervision path (liveness loop, sidecar reading, kill,
classification) without any LLM or domain knowledge. The stub writes exactly the
envelope it is told to (StubRunner emit=...) or hangs (sleep=True). They are
integration tests because they cross a process boundary.
"""

import json

import pytest

from agent_flow.core.agent_runtime import run_agent
from agent_flow.runners.executor import AgentContentFailedError, AgentTimeoutError

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
    assert result.result_errors
