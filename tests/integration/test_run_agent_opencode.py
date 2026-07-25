"""Real-opencode integration test (opt-in) — one minimal agent, end to end.

Runs ONE real opencode agent (the `selftest-analyst` fixture) through run_agent
and asserts it wrote its report + a valid sidecar, and that token telemetry was
harvested.

OPT-IN — enable with:  AGENT_FLOW_OPENCODE_E2E=1
Prerequisites (see design doc §14):
  - opencode on PATH,
  - working provider credentials in the process env (via .env / exported), and
  - run OUTSIDE an opencode session (nested opencode invocation errors with an
    opencode UnknownError — a runtime artifact, not a library issue).
Optionally set AGENT_FLOW_E2E_MODEL to force a model; unset -> no --model, so
opencode resolves the model from its own config.

Skipped by default so unit + mock-integration runs stay green everywhere.
"""

import os
import shutil

import pytest

from agent_flow.core.agent_runtime import run_agent
from agent_flow.runners import OpenCodeRunner

pytestmark = pytest.mark.skipif(
    shutil.which("opencode") is None or os.environ.get("AGENT_FLOW_OPENCODE_E2E") != "1",
    reason="real opencode e2e not enabled (set AGENT_FLOW_OPENCODE_E2E=1, run outside opencode)",
)


def test_opencode_single_stage_writes_report_and_sidecar(opencode_workspace):
    # agent_dir = project root (where .opencode/ lives) -> opencode --dir finds
    # the agent + provider config regardless of cwd. run_dir/outputs go to a
    # project-local temp dir. These are two INDEPENDENT directories.
    project_dir, outdir = opencode_workspace
    report = outdir / "report.md"
    control = outdir / "selftest-analyst.control.json"
    model = os.environ.get("AGENT_FLOW_E2E_MODEL")  # None -> runner default

    result = run_agent(
        agent="selftest-analyst",
        prompt=f"PRODUCT_KEY: agent-flow-selftest\nREPORT: {report}\nCONTROL_FILE: {control}\n",
        run_dir=outdir,
        agent_dir=project_dir,
        runner=OpenCodeRunner(),
        model=model,
        idle_timeout_s=120,  # real LLM: generous idle window
        control_file=control,
    )

    assert result.control["status"] == "ok"
    assert report.exists() and report.stat().st_size > 0
    assert control.exists()
    assert "agent-flow-selftest" in report.read_text()
    assert result.tokens > 0  # telemetry harvested from the event stream
