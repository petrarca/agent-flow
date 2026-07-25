"""Unit tests for the resolved-params summary: runtime-populated fields are hidden."""

from pydantic import Field
from pydantic_settings import BaseSettings

from agent_flow import runtime_param, runtime_param_fields
from agent_flow.cli import _print_run_summary


class _Params(BaseSettings):
    product_key: str = "demo"
    # runtime-populated via the agent-flow helper (not a hand-typed marker).
    analysis_timestamp: str = Field(default="UNKNOWN", json_schema_extra=runtime_param())
    pipeline_commit: str = Field(default="UNKNOWN", json_schema_extra=runtime_param())


class _Cfg:
    runtime = "mock"
    agent_dir = ""
    run_dir = ""
    model = ""
    idle_timeout_s = 120


def test_runtime_param_helper_shape():
    assert runtime_param() == {"runtime": True}
    assert runtime_param(examples=["x"]) == {"runtime": True, "examples": ["x"]}


def test_runtime_param_fields_detects_tagged_fields():
    assert runtime_param_fields(_Params) == {"analysis_timestamp", "pipeline_commit"}


def test_runtime_param_fields_none_or_untagged():
    assert runtime_param_fields(None) == set()

    class Plain(BaseSettings):
        a: str = "1"

    assert runtime_param_fields(Plain) == set()


def test_summary_hides_runtime_fields(capsys):
    from rich.console import Console

    console = Console(force_terminal=False)
    params = {"product_key": "demo", "analysis_timestamp": "UNKNOWN", "pipeline_commit": "UNKNOWN"}
    _print_run_summary("t", _Cfg(), params, console, hide=runtime_param_fields(_Params))
    out = capsys.readouterr().out
    assert "product_key" in out
    assert "analysis_timestamp" not in out
    assert "pipeline_commit" not in out


def test_summary_shows_all_when_nothing_hidden(capsys):
    from rich.console import Console

    console = Console(force_terminal=False)
    params = {"product_key": "demo", "analysis_timestamp": "UNKNOWN"}
    _print_run_summary("t", _Cfg(), params, console)
    out = capsys.readouterr().out
    assert "analysis_timestamp" in out  # not hidden without the marker
