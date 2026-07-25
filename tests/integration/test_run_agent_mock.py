"""Integration tests for run_agent via the MockRunner.

These spawn the mock agent stub as a real subprocess and exercise the full
supervision path (liveness loop, sidecar reading, kill, classification) without
any LLM. They are integration tests because they cross a process boundary.
"""

import json

import pytest

from agent_flow.core.agent_runtime import AgentContentFailedError, AgentTimeoutError, run_agent
from agent_flow.runners import MockRunner


def _prompt(report, control):
    return f"PRODUCT_KEY: test\nREPORT: {report}\nCONTROL_FILE: {control}"


def test_run_agent_ok(tmp_path):
    report = tmp_path / "tech-stack.md"
    control = tmp_path / "tech-stack-analyst.control.json"
    result = run_agent(
        agent="tech-stack-analyst",
        prompt=_prompt(report, control),
        run_dir=tmp_path,
        runner=MockRunner(),
        control_file=control,
    )
    assert result.control["status"] == "ok"
    assert report.exists()
    assert control.exists()


def test_run_agent_content_failure_not_retryable(tmp_path, monkeypatch):
    # MOCK_FAIL makes the stub write a status:error SIDECAR + exit non-zero.
    monkeypatch.setenv("MOCK_FAIL", "tech-stack-analyst")
    report = tmp_path / "tech-stack.md"
    control = tmp_path / "tech-stack-analyst.control.json"
    with pytest.raises(AgentContentFailedError):
        run_agent(
            agent="tech-stack-analyst",
            prompt=_prompt(report, control),
            run_dir=tmp_path,
            runner=MockRunner(),
            control_file=control,
        )
    # The failure came from an actual error SIDECAR (not merely a missing one),
    # matching how a real agent reports a content failure.
    assert json.loads(control.read_text())["status"] == "error"


def test_run_agent_stale_timeout(tmp_path, monkeypatch):
    # MOCK_HANG makes the stub sleep forever; with a tiny idle window and no
    # sidecar, supervision must declare it stale and kill it.
    monkeypatch.setenv("MOCK_HANG", "domain-analyst")
    report = tmp_path / "domain.md"
    control = tmp_path / "domain-analyst.control.json"
    with pytest.raises(AgentTimeoutError):
        run_agent(
            agent="domain-analyst",
            prompt=_prompt(report, control),
            run_dir=tmp_path,
            runner=MockRunner(),
            idle_timeout_s=2,
            control_file=control,
        )


def test_run_agent_sidecar_is_authoritative(tmp_path):
    # A pre-existing (stale) sidecar must be cleared before the run, so the
    # result reflects THIS run's sidecar, not the old one.
    report = tmp_path / "coupling.md"
    control = tmp_path / "coupling-analyst.control.json"
    control.write_text(json.dumps({"status": "error", "reason": "stale"}))
    result = run_agent(
        agent="coupling-analyst",
        prompt=_prompt(report, control),
        run_dir=tmp_path,
        runner=MockRunner(),
        control_file=control,
    )
    assert result.control["status"] == "ok"  # fresh sidecar, not the stale error


def test_run_agent_validates_result_schema_valid(tmp_path):
    # The mock tech analyst emits result {summary, languages}; a matching schema
    # must validate and attach the outcome (engine never fails on it).
    report = tmp_path / "tech-stack.md"
    control = tmp_path / "tech-stack-analyst.control.json"
    schema = {
        "type": "object",
        "properties": {"summary": {"type": "string"}, "languages": {"type": "array"}},
        "required": ["summary", "languages"],
    }
    result = run_agent(
        agent="tech-stack-analyst",
        prompt=_prompt(report, control),
        run_dir=tmp_path,
        runner=MockRunner(),
        control_file=control,
        result_schema=schema,
    )
    assert result.control["status"] == "ok"
    assert result.result_valid is True
    assert result.result_errors == ()


def test_run_agent_flags_invalid_result_without_failing(tmp_path):
    # A schema the mock output does NOT satisfy: run still succeeds (status ok),
    # but result_valid is False so a gate can decide what to do.
    report = tmp_path / "tech-stack.md"
    control = tmp_path / "tech-stack-analyst.control.json"
    schema = {"type": "object", "properties": {"nope": {"type": "string"}}, "required": ["nope"]}
    result = run_agent(
        agent="tech-stack-analyst",
        prompt=_prompt(report, control),
        run_dir=tmp_path,
        runner=MockRunner(),
        control_file=control,
        result_schema=schema,
    )
    assert result.control["status"] == "ok"  # engine did NOT fail the run
    assert result.result_valid is False
    assert result.result_errors
