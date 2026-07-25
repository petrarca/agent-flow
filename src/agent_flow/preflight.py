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


def check_agent_dir_exists(agent_dir: str | Path | None) -> Check:
    """The agent-definitions dir must be configured and exist (runtime-agnostic).

    The specific LAYOUT a runtime expects inside it (e.g. opencode's
    `.opencode/agent*`) is checked by that runtime's runner via
    `preflight_checks` — this generic check only verifies it is set and is a dir.
    """
    if not agent_dir:
        return Check("agent-dir", False, True, "no agent_dir configured (set --agent-dir / AGENT_FLOW_AGENT_DIR / default_agent_dir).")
    base = Path(agent_dir)
    if not base.is_dir():
        return Check("agent-dir", False, True, f"agent_dir does not exist: {base}")
    return Check("agent-dir", True, True, f"agent_dir at {base}")


def check_prefect_importable() -> Check:
    """Prefect must import (only relevant when the Prefect backend is selected)."""
    try:
        __import__("prefect")
    except ImportError as exc:
        return Check("prefect-importable", False, True, f"cannot import prefect: {exc}")
    return Check("prefect-importable", True, True, "prefect importable")


def check(runtime: str, agent_dir: str | Path | None, backend: str = "local") -> list[Check]:
    """Run the pre-flight checks relevant to `runtime`/`backend` and return them.

    Layers, so no runtime/backend specifics live here:
    - GENERIC (always): agent_dir configured/exists.
    - BACKEND-SPECIFIC: the Prefect backend additionally requires prefect to
      import; the local backend needs nothing (no check added). So a
      dependency-light local run does not fail merely because Prefect is absent.
    - RUNTIME-SPECIFIC: whatever the selected runner contributes via its optional
      `preflight_checks(agent_dir)` — e.g. OpenCodeRunner checks opencode is on
      PATH, we are not nested in an opencode session, and the `.opencode/agent*`
      layout. A runner without the method (mock, or a minimal one) contributes
      nothing. This is the seam that lets a new runtime (Claude Code, …) declare
      its own pre-conditions without touching this module.

    Returns all outcomes (passing and failing); the caller decides based on
    `.fatal` and `.ok`. An unknown runtime contributes no runner checks (its
    absence surfaces later); the generic checks still run.
    """
    from agent_flow.runners import get_runner

    results: list[Check] = [check_agent_dir_exists(agent_dir)]
    if backend == "prefect":
        results.insert(0, check_prefect_importable())
    try:
        runner = get_runner(runtime)
    except ValueError:
        return results  # unknown runtime -> only the generic/backend checks
    runner_checks = getattr(runner, "preflight_checks", None)
    if callable(runner_checks):
        results.extend(runner_checks(agent_dir))
    return results


def fatal_failures(results: list[Check]) -> list[Check]:
    """The subset of results that are fatal AND failed (i.e. reasons not to start)."""
    return [c for c in results if c.fatal and not c.ok]
