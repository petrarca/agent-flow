"""Directory-based test auto-marking (petrarca ADR-0002).

Anything under tests/integration/ is marked `integration`; everything else is
marked `unit`. No test file needs an explicit marker — classification follows
location, so it cannot be forgotten. The default run is `-m unit` (fast,
docker-free); integration tests run via `-m integration`.
"""

import json
import shutil
import tempfile
from pathlib import Path

import pytest

from agent_flow.runners.base import AgentInvocation, AgentRunnerInfo, Event

FIXTURES = Path(__file__).parent / "fixtures"

_STUB = Path(__file__).resolve().parents[1] / "src" / "agent_flow" / "core" / "_mock_agent.py"


class StubRunner:
    """Test-only AgentRunner that drives the domain-free subprocess stub.

    Exercises the real SubprocessExecutor path (spawn / supervise / kill /
    sidecar) with no tokens and no domain knowledge. The behaviour is supplied
    entirely on the command line:
      - emit=<dict>: the stub writes that envelope as the sidecar and exits 0.
      - sleep=True:  the stub hangs forever (tests the stale/kill path).
    """

    name = "stub"

    def __init__(self, *, emit: dict | None = None, sleep: bool = False) -> None:
        self._emit = emit
        self._sleep = sleep

    def build_command(self, inv: AgentInvocation) -> list[str]:
        cmd = ["python3", str(_STUB), "--agent", inv.agent, "--prompt", inv.prompt]
        if self._sleep:
            cmd.append("--sleep")
        elif self._emit is not None:
            cmd += ["--emit", json.dumps(self._emit)]
        return cmd

    def parse_event(self, line: str) -> Event:
        return Event.none()  # stub finishes fast; completion is via sidecar

    def info(self, agent_dir=None) -> AgentRunnerInfo:  # noqa: ARG002
        return AgentRunnerInfo(name=self.name, available=True, detail="test stub")


@pytest.fixture
def stub_runner() -> type[StubRunner]:
    """The StubRunner CLASS (not an instance) — instantiate per test, e.g.
    `stub_runner(emit={...})` or `stub_runner(sleep=True)`.

    Exposed as a fixture (not a plain import) so it resolves identically
    regardless of how pytest is invoked (`python -m pytest` vs the `pytest`
    console-script entry point) — `tests/` has no `__init__.py`, so
    `from tests.conftest import StubRunner` is import-mode-dependent; pytest's
    fixture injection is not.
    """
    return StubRunner


def pytest_collection_modifyitems(config, items):  # noqa: ARG001
    integration_root = Path(__file__).parent / "integration"
    for item in items:
        path = Path(str(item.fspath))
        marker = "integration" if integration_root in path.parents else "unit"
        item.add_marker(marker)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def opencode_workspace():
    """Set up so a real opencode run resolves the working `.opencode/` config.

    Empirically, opencode's Foundry (azure-claude) provider only works when
    opencode runs with cwd inside a directory that inherits the project's
    `.opencode/` config context. A bare temp dir with its OWN `.opencode/`
    becomes an isolated opencode project that lacks that config and fails with
    `UnknownError`. (This is also why the real tech DAG works: its run_dir has
    no local `.opencode/`, so opencode walks up to the project's.)

    So: install the FIXTURE agent into the PROJECT's `.opencode/agent/` for the
    test's duration, and hand the test a project-local output dir. Yields
    (run_dir=project_root, outdir). Cleans up the installed agent afterwards.
    """
    installed = PROJECT_ROOT / ".opencode" / "agent" / "selftest-analyst.md"
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / "opencode" / "agent" / "selftest-analyst.md", installed)
    outdir = Path(tempfile.mkdtemp(dir=PROJECT_ROOT))
    try:
        yield PROJECT_ROOT, outdir
    finally:
        installed.unlink(missing_ok=True)
        shutil.rmtree(outdir, ignore_errors=True)
