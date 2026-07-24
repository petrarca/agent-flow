"""Directory-based test auto-marking (petrarca ADR-0002).

Anything under tests/integration/ is marked `integration`; everything else is
marked `unit`. No test file needs an explicit marker — classification follows
location, so it cannot be forgotten. The default run is `-m unit` (fast,
docker-free); integration tests run via `-m integration`.
"""

import shutil
import tempfile
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


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
