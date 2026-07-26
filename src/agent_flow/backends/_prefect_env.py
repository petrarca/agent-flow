"""Prefect environment bootstrap — MUST be imported before `prefect`.

Fixes the intermittent `sqlite3.OperationalError: database is locked` seen with
Prefect's ephemeral (temporary-server) mode. Causes and fixes:

  1. A shared/global PREFECT_HOME whose SQLite file is contended. -> pin a
     project-local PREFECT_HOME so this run owns its own database.
  2. SQLite's default busy timeout is too short under the brief write burst at
     server startup. -> raise the connection busy timeout.
  3. Client-side telemetry/metrics write to the same DB during startup and race
     the flow's first writes. -> disable them (not needed for a local run).

These are set as environment variables because Prefect reads its settings from
the environment at import time; setting them here (before any `prefect` import)
guarantees they take effect.

Testability (#12): bootstrap() uses os.environ.setdefault throughout, so it is a
no-op for any key already set. Test code can set PREFECT_HOME (and other keys)
before importing flow.py to avoid the directory-creation side effect and to point
Prefect at a temp directory:

    import os, tempfile
    os.environ["PREFECT_HOME"] = tempfile.mkdtemp()
    from agent_flow.flow import pipeline  # bootstrap() is now a no-op
"""

from __future__ import annotations

import os
from pathlib import Path


def _db_url(prefect_home: Path) -> str:
    """Build the SQLite connection URL with forward slashes (POSIX + Windows safe).

    Path.__truediv__ gives OS-native separators; SQLite URLs always need
    forward slashes, so we normalise explicitly (#11).
    """
    db_path = (prefect_home / "prefect.db").as_posix()
    return f"sqlite+aiosqlite:///{db_path}"


def bootstrap() -> None:
    """Set Prefect env defaults for a robust, self-contained local run.

    Three modes, selected by environment:

    - **Mode 2 (persistent server):** if PREFECT_API_URL is set, record to that
      server (UI + history) — do NOT touch the embedded DB config.
    - **Embedded, in-memory (default):** ephemeral in-process server backed by an
      **in-memory SQLite** DB. Fastest, no file, no lock contention. State lives
      only for the run — which is all we need, because resume is handled by our
      own per-stage sidecar files on disk, not by Prefect's DB. Best for
      one-shot batch runs and CI.
    - **Embedded, file-backed (opt-in):** set PREFECT_PERSIST=1 to use a
      project-local file SQLite instead (if you want Prefect's own run history to
      survive across separate invocations without standing up a server).

    Idempotent: every key is set with setdefault, so a key already present in the
    environment (e.g. set by test code) is never overwritten.
    """
    # Mode 2: persistent server — leave the DB alone, just quiet telemetry.
    if os.environ.get("PREFECT_API_URL"):
        os.environ.setdefault("PREFECT_CLIENT_METRICS_ENABLED", "false")
        return

    common = {
        "PREFECT_SERVER_ANALYTICS_ENABLED": "false",
        "PREFECT_CLIENT_METRICS_ENABLED": "false",
        "PREFECT_LOGGING_TO_API_ENABLED": "false",
    }

    if os.environ.get("PREFECT_PERSIST") == "1":
        # Embedded, file-backed: project-local .prefect/prefect.db.
        project_root = Path(__file__).resolve().parents[2]
        prefect_home = project_root / ".prefect"
        if "PREFECT_HOME" not in os.environ:
            prefect_home.mkdir(parents=True, exist_ok=True)
        defaults = {
            "PREFECT_HOME": str(prefect_home),
            "PREFECT_API_DATABASE_CONNECTION_URL": _db_url(prefect_home),
            "PREFECT_API_DATABASE_TIMEOUT": "30",
            "PREFECT_API_DATABASE_CONNECTION_TIMEOUT": "30",
            **common,
        }
    else:
        # Embedded, in-memory (default): fastest, no file, no lock races.
        defaults = {
            "PREFECT_API_DATABASE_CONNECTION_URL": "sqlite+aiosqlite:///:memory:",
            **common,
        }

    for key, value in defaults.items():
        os.environ.setdefault(key, value)
