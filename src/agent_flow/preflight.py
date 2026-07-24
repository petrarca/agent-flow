"""Pre-flight checks — fail fast, before any agent subprocess is spawned.

A pipeline run spends real time and tokens the moment it launches an agent. The
cheap, deterministic failures — the runtime binary is missing, we are nested
inside an opencode session (which makes the child opencode raise UnknownError),
the agent-definitions dir does not exist, the Prefect backend is unreachable —
should surface IMMEDIATELY, not three nodes into a run.

This module owns the GENERIC, runtime/infrastructure checks. It does NOT know
about any pipeline's domain preconditions (which tech-assessment reports exist,
is CocoRAG indexed, …) — those belong to the consumer, expressed as a
pydantic-settings params model (required fields, DirectoryPath, validators) and
as the pipeline's own first node.

`check(...)` returns a list of `Check` results (no exceptions); the caller
(run_cli) decides what to do. `any(c.fatal and not c.ok for c in results)` means
"do not start". This keeps the module testable and lets a caller render all
failures at once rather than one-at-a-time.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Check:
    """One pre-flight check outcome.

    name    short identifier (e.g. "opencode-installed").
    ok      True if the check passed.
    fatal   True if a failure must abort the run (vs. a non-blocking warning).
    detail  human-readable explanation (why it failed, or what was found).
    """

    name: str
    ok: bool
    fatal: bool
    detail: str


def check_opencode_installed() -> Check:
    """opencode must be on PATH and executable (only relevant for the real runtime)."""
    path = shutil.which("opencode")
    if path:
        return Check("opencode-installed", True, True, f"found at {path}")
    return Check(
        "opencode-installed",
        False,
        True,
        "opencode not found on PATH. Install it and ensure `opencode` is executable.",
    )


def check_not_nested_session() -> Check:
    """Refuse to run inside an active opencode session (nested -> UnknownError).

    opencode marks an active session with OPENCODE=1 (and OPENCODE_PID). Spawning
    a child opencode from within one fails with UnknownError, so this is fatal for
    the opencode runtime.
    """
    if os.environ.get("OPENCODE") == "1":
        return Check(
            "not-nested-session",
            False,
            True,
            "Running inside an opencode session (OPENCODE=1). Start the pipeline from a normal shell, outside opencode.",
        )
    return Check("not-nested-session", True, True, "not inside an opencode session")


def check_agent_dir(agent_dir: str | Path | None) -> Check:
    """The agent-definitions dir must exist and contain an .opencode/agent* dir."""
    if not agent_dir:
        return Check("agent-dir", False, True, "no agent_dir configured (set --agent-dir / AGENT_FLOW_AGENT_DIR / default_agent_dir).")
    base = Path(agent_dir)
    if not base.is_dir():
        return Check("agent-dir", False, True, f"agent_dir does not exist: {base}")
    opencode_dir = base / ".opencode"
    has_agents = opencode_dir.is_dir() and any(opencode_dir.glob("agent*"))
    if not has_agents:
        return Check("agent-dir", False, True, f"no .opencode/agent* directory under {base} (opencode --dir target).")
    return Check("agent-dir", True, True, f"agent definitions at {opencode_dir}")


def check_prefect_importable() -> Check:
    """Prefect must import (it is a core dependency and drives the flow engine)."""
    try:
        __import__("prefect")
    except ImportError as exc:  # pragma: no cover - prefect is a core dep
        return Check("prefect-importable", False, True, f"cannot import prefect: {exc}")
    return Check("prefect-importable", True, True, "prefect importable")


def check(runtime: str, agent_dir: str | Path | None) -> list[Check]:
    """Run the pre-flight checks relevant to `runtime` and return their outcomes.

    - The opencode runtime needs opencode installed, a non-nested session, and a
      valid agent_dir.
    - The mock runtime skips the opencode-specific checks (no binary, nesting is
      harmless), but still needs a valid agent_dir (agents are still resolved
      from .opencode/agent*).
    - Prefect is always checked (it powers the engine for every runtime).

    Returns all outcomes (passing and failing); the caller decides based on
    `.fatal` and `.ok`.
    """
    results: list[Check] = [check_prefect_importable(), check_agent_dir(agent_dir)]
    if runtime == "opencode":
        results.append(check_opencode_installed())
        results.append(check_not_nested_session())
    return results


def fatal_failures(results: list[Check]) -> list[Check]:
    """The subset of results that are fatal AND failed (i.e. reasons not to start)."""
    return [c for c in results if c.fatal and not c.ok]
