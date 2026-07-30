"""Directory-based test auto-marking (petrarca ADR-0002).

Anything under tests/integration/ is marked `integration`; everything else is
marked `unit`. No test file needs an explicit marker — classification follows
location, so it cannot be forgotten. The default run is `-m unit` (fast,
docker-free); integration tests run via `-m integration`.
"""

import json
import os
import shlex
import shutil
import tempfile
from pathlib import Path

# Prefect reads its settings ONCE, at first import, so a PREFECT_* set after
# that is ignored for the life of the process. Several tests import prefect
# (one of them deletes it from sys.modules and re-imports it), so the first
# import can happen before any backend calls bootstrap(). With the API log
# handler left enabled, prefect queues records for an API that does not exist
# and reports the failure at interpreter exit — "Error logging to API / All
# connection attempts failed" printed after the pytest summary. Set here, at
# conftest import, which precedes every test module.
os.environ.setdefault("PREFECT_LOGGING_TO_API_ENABLED", "false")

import pytest

from agent_flow.runners.base import AgentInvocation, AgentRunnerInfo, Event, LaunchSpec

FIXTURES = Path(__file__).parent / "fixtures"

_STUB = Path(__file__).resolve().parents[1] / "src" / "agent_flow" / "core" / "_mock_agent.py"


class StubRunner:
    """Test-only AgentRunner that drives the domain-free subprocess stub.

    Exercises the real SubprocessExecutor path (spawn / supervise / kill /
    sidecar) with no tokens and no domain knowledge. The behaviour is supplied
    entirely on the command line:
      - emit=<dict>: the stub writes that envelope as the sidecar and exits 0.
      - sleep=True:  the stub hangs forever (tests the stale/kill path).
      - print_line=<str> + exit_code=<int>: print a raw stdout line, write NO
        sidecar, exit with the code (tests the no-sidecar diagnostic + routing).
    """

    name = "stub"

    def __init__(self, *, emit: dict | None = None, sleep: bool = False, print_line: str = "", exit_code: int = 0) -> None:
        self._emit = emit
        self._sleep = sleep
        self._print = print_line
        self._exit = exit_code

    def build_command(self, inv: AgentInvocation) -> LaunchSpec:
        argv = ["python3", str(_STUB), "--agent", inv.agent, "--prompt", inv.prompt]
        if self._sleep:
            argv.append("--sleep")
        elif self._print or self._exit:
            if self._print:
                argv += ["--print", self._print]
            if self._exit:
                argv += ["--exit-code", str(self._exit)]
        elif self._emit is not None:
            argv += ["--emit", json.dumps(self._emit)]
        display = shlex.join(argv[:5]) + " <prompt elided>"
        return LaunchSpec(argv=argv, display=display)

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


@pytest.fixture
def anyio_backend() -> str:
    """Pin the anyio pytest plugin to the asyncio backend for `@pytest.mark.anyio`
    tests. The engine only targets asyncio (Starlette/Prefect hosts); we do not
    run the suite on trio, so a single-backend fixture keeps async tests
    unparametrized.
    """
    return "asyncio"


@pytest.fixture(scope="session", autouse=True)
def _stop_prefect_ephemeral_server():
    """Stop Prefect's temporary subprocess server BEFORE the interpreter exits.

    The `prefect` integration param spins Prefect's ephemeral ASGI server, which
    registers its own `stop()` via `atexit`. That atexit runs AFTER pytest has
    already closed the captured stdout/stderr, so its "Stopping temporary server"
    log write hits a closed stream and prints a `ValueError: I/O operation on
    closed file` logging-error block (harmless, but noisy — and it looks like a
    failure). Stopping the server here, during session teardown while the streams
    are still open, makes the later atexit `stop()` a no-op and keeps shutdown
    clean. No-op when Prefect is absent (unit-only runs) or was never started.
    """
    yield
    try:
        from prefect.server.api.server import SubprocessASGIServer
    except Exception:  # noqa: BLE001 - prefect not installed: nothing to stop
        return
    # Stop every started instance (keyed by port); stop() is idempotent and
    # clears the process, so the atexit-registered stop() then does nothing.
    for server in list(getattr(SubprocessASGIServer, "_instances", {}).values()):
        try:
            server.stop()
        except Exception:  # noqa: BLE001 - best-effort teardown; never fail the session
            pass


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


class _ExecutorSpy:
    """What `spy_executor` hands a test: the last invocation + get_executor kwargs.

    `inv` is the neutral AgentInvocation the node built (assert on .model,
    .agent_dir, .idle_timeout_s, .prompt, …); `kwargs` are what node_builder
    passed to `get_executor` (assert on options=…). `by_node` keeps every
    invocation keyed by node name for multi-node flows.
    """

    def __init__(self) -> None:
        self.inv = None
        self.kwargs: dict = {}
        self.by_node: dict = {}


@pytest.fixture
def spy_executor(monkeypatch):
    """Stub the EXECUTOR seam and record what the node builder produced.

    One shared harness instead of a hand-rolled fake per test module: when the
    seam's signature changes (as when `options=` was added to `get_executor`),
    exactly one place needs updating — previously the same lambda had to be
    patched in four files. Accepts **kwargs so a new seam argument never breaks
    every caller again.
    """
    from agent_flow.core.agent_runtime import AgentResult

    spy = _ExecutorSpy()

    class _FakeExecutor:
        name = "fake"

        async def run(self, inv):
            spy.inv = inv
            spy.by_node[inv.node] = inv
            return AgentResult(agent=inv.agent, exit_code=0, duration_s=0.0, control={"status": "ok"}, completion="completed")

    def _get_executor(_runtime, **kwargs):
        spy.kwargs = kwargs
        return _FakeExecutor()

    monkeypatch.setattr("agent_flow.node_builder.executor_choice.get_executor", _get_executor)
    return spy
