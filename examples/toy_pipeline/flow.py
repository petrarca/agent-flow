"""Deterministic Prefect flow: analyst -> verifier -> extractor.

LAYER 2 (primitives) example: this flow is HAND-WRITTEN with Prefect's own
@flow/@task, and calls the library primitive `run_agent` directly as the leaf of
each stage. It does NOT use `agent_flow.build_flow` — you own the flow shape,
the library only supervises each agent subprocess. Contrast with
examples/tech_assessment, which is the LAYER 3 (declaration + build_flow) model:
declare Nodes, let the engine build the flow. Both are valid; pick per pipeline.

This is the "graph, not a loop" (per Osmani/Horthy): the control flow lives in
plain Python that Prefect executes and supervises. No LLM sequences the stages.
Each stage:

  - runs one opencode agent as a supervised subprocess (hard timeout -> kill),
  - is wrapped in a Prefect task with retries (a hung/failed agent is retried
    automatically instead of needing a manual restart),
  - records its outcome to a per-stage JSON file so a crashed run RESUMES from
    the last completed stage instead of starting over (#9: TOCTOU-safe).

Model choice is owned here (the orchestrator), not in the agent .md files:
each StageConfig may pin a model; unspecified stages pass no model -> the runtime resolves it.

Run it:
    uv run --with prefect python -m agent_flow.flow --topic "Hexagonal architecture" --runtime mock
    uv run --with prefect python -m agent_flow.flow --topic "Hexagonal architecture" --runtime opencode
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

# MUST run before `prefect` is imported: pins a project-local PREFECT_HOME and
# disables the startup telemetry races that cause "database is locked".
# #12: bootstrap() is a no-op when PREFECT_HOME is already set, so test code
# can set that env var before importing this module to avoid the side effects.
from agent_flow._prefect_env import bootstrap

bootstrap()

from prefect import flow, get_run_logger, task  # noqa: E402
from prefect.tasks import exponential_backoff  # noqa: E402
from prefect.transactions import transaction  # noqa: E402

from agent_flow.core.agent_runtime import (  # noqa: E402
    AgentContentFailedError,
    AgentResult,
    run_agent,
)
from agent_flow.runners import MockRunner, OpenCodeRunner  # noqa: E402

# This example's own opencode project dir (holds .opencode/agent/*.md).
_PACKAGE_DIR = Path(__file__).resolve().parent  # noqa: E402

# Tasks tagged with this get bounded by the concurrency limit set inside the
# flow, so a parallel fan-out of agents cannot exceed the model provider's
# rate limit.
LLM_TAG = "llm"


# Retry policy (#6: isinstance-based, not message-string-based)


def _is_transient(exc: BaseException) -> bool:
    """True when the failure is transient and worth retrying.

    Uses isinstance on the two distinct exception subclasses introduced in
    agent_runtime — not string-matching on the message — so this remains
    correct even if error messages change.

      AgentTimeoutError          -> transient (hang); retry.
      AgentCrashError            -> transient (process crash / rate-limit); retry.
      AgentContentFailedError    -> genuine content failure; do NOT retry.
      anything else              -> unknown infra error; retry.
    """
    if isinstance(exc, AgentContentFailedError):
        return False
    return True  # AgentTimeoutError, AgentCrashError, unknown


def _retry_condition(task, task_run, state) -> bool:  # noqa: ANN001 - Prefect internals
    """Prefect retry_condition_fn wired to _is_transient."""
    try:
        state.result(raise_on_failure=True)
    except BaseException as exc:  # noqa: BLE001 - we classify, we don't handle
        return _is_transient(exc)
    return False


# Stage configuration


@dataclass(frozen=True)
class StageConfig:
    """Per-stage orchestration policy — the orchestrator owns all of this."""

    name: str
    agent: str
    timeout_s: int
    retries: int
    model: str | None = None  # None -> no --model; runtime resolves it


# The pipeline graph. Edit models/timeouts/retries here — agents stay agnostic.
# Pin a cheaper model per stage when speed matters more than depth, e.g.:
#   StageConfig("extract", "extractor", 300, 2, model="azure-claude/Claude Haiku 4.5")
STAGES: dict[str, StageConfig] = {
    "analyze": StageConfig(name="analyze", agent="analyst", timeout_s=600, retries=2),
    "verify": StageConfig(name="verify", agent="verifier", timeout_s=600, retries=2),
    "extract": StageConfig(name="extract", agent="extractor", timeout_s=600, retries=2),
}


# Durable state — per-stage files (#9: TOCTOU-safe for parallel fan-out)


def _stage_state_path(run_dir: Path, stage: str) -> Path:
    """One JSON file per stage — no shared file, no read-modify-write race."""
    return run_dir / f"_state_{stage}.json"


def _stage_done(run_dir: Path, stage: str) -> dict | None:
    """Return the saved control record if this stage is already complete."""
    p = _stage_state_path(run_dir, stage)
    if p.exists():
        return json.loads(p.read_text()).get("control")
    return None


def _save_stage(run_dir: Path, stage: str, result: AgentResult) -> None:
    """Atomically record a completed stage (write-then-rename for safety)."""
    data = {
        "agent": result.agent,
        "exit_code": result.exit_code,
        "duration_s": round(result.duration_s, 2),
        "control": result.control,
    }
    target = _stage_state_path(run_dir, stage)
    tmp = target.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(target)  # atomic on POSIX; best-effort on Windows


# Task factory (#10: build the configured task variant once per stage)


def _make_stage_task(cfg: StageConfig):  # noqa: ANN201
    """Return a Prefect task pre-configured for this stage.

    Calling .with_options() creates a new task-definition object; doing it
    once here (at flow-definition time) avoids rebuilding it on every
    invocation.
    """
    return run_stage.with_options(
        name=f"run_stage_{cfg.name}",
        retries=cfg.retries,
        retry_delay_seconds=exponential_backoff(backoff_factor=10),
        timeout_seconds=cfg.timeout_s + 60,
    )


# Base task


# Base task. Per-stage retry COUNT/DELAY are baked into _STAGE_TASKS below;
# the retry CONDITION, tag (for concurrency limiting), and jitter are fixed
# here because they don't vary per stage.
@task(
    retries=0,
    retry_condition_fn=_retry_condition,
    retry_jitter_factor=0.5,
    tags=[LLM_TAG],
)
def run_stage(
    *,
    stage: StageConfig,
    prompt: str,
    run_dir: Path,
    runtime: str,
    force: bool,
    show_events: bool = False,
) -> dict:
    """Run one supervised agent stage, honouring resume (skip-if-done).

    #8: artifact is no longer a parameter — rollback uses the flow-level
    transaction key set by the flow, avoiding the false impression that
    this task sets its own per-invocation key.
    """
    logger = get_run_logger()

    if not force:
        done = _stage_done(run_dir, stage.name)
        if done is not None:
            logger.info("stage %s already complete -> resuming (skip)", stage.name)
            return done

    # None -> no --model; the runtime resolves the model from its own config.
    model = stage.model
    logger.info("stage %s: agent=%s model=%s timeout=%ss", stage.name, stage.agent, model or "(runtime default)", stage.timeout_s)

    # Optional live-event display (Prefect INFO logs remain the diagnostics).
    on_event = None
    if show_events:
        from agent_flow.cli import event_printer

        on_event = event_printer(stage.agent)

    # Derive per-agent sidecar path (agent-specific so analyst and verifier
    # on the same report each have their own receipt).
    control_file = run_dir / f"{stage.agent}.control.json"
    runner = OpenCodeRunner() if runtime == "opencode" else MockRunner()
    result = run_agent(
        agent=stage.agent,
        prompt=prompt,  # control-file protocol is injected by run_agent
        run_dir=run_dir,
        # agent definitions live in this example's own .opencode/ (opencode --dir).
        agent_dir=_PACKAGE_DIR,
        runner=runner,
        idle_timeout_s=stage.timeout_s,
        model=model,
        control_file=control_file,
        on_event=on_event,
    )
    logger.info("stage %s ok in %.1fs -> %s", stage.name, result.duration_s, result.control)
    _save_stage(run_dir, stage.name, result)
    return result.control


# Pre-built task variants — keyed by stage name.  Built AFTER run_stage is
# defined so .with_options() can reference it (#10: built once, not per call).
_STAGE_TASKS = {name: _make_stage_task(cfg) for name, cfg in STAGES.items()}


@run_stage.on_rollback
def _rollback_stage(txn) -> None:  # noqa: ANN001 - Prefect Transaction
    """Delete the current artifact when the enclosing transaction rolls back.

    The flow sets txn["artifact"] to the path most recently written before
    each stage, so rollback always targets the right file.
    """
    artifact = txn.get("artifact")
    if artifact:
        Path(artifact).unlink(missing_ok=True)


# Concurrency limit (#7: applied inside the flow, after the server is running)


def _apply_concurrency_limit(limit: int) -> None:
    """Create or ensure the tag-based concurrency limit for LLM_TAG.

    Must be called INSIDE a flow run (or with a running Prefect server) because
    in ephemeral mode the server does not exist until the flow starts.
    Idempotent; tolerates the limit already existing.
    """
    import anyio
    import httpx
    from prefect.client.orchestration import get_client
    from prefect.exceptions import PrefectException

    async def _create() -> None:
        async with get_client() as client:
            await client.create_concurrency_limit(tag=LLM_TAG, concurrency_limit=limit)

    logger = get_run_logger()
    try:
        anyio.run(_create)
        logger.info("LLM concurrency limit set to %d (tag=%r)", limit, LLM_TAG)
    except (PrefectException, httpx.HTTPError, OSError) as exc:
        logger.warning("concurrency limit setup skipped: %s", exc)


# Flow (the graph)


@flow(name="analyst-verifier-extractor")
def pipeline(
    topic: str,
    run_dir: str = "",
    runtime: str = "opencode",
    force: bool = False,
    llm_concurrency: int = 3,
    show_events: bool = False,
) -> dict:
    """Run the three-stage pipeline for one topic.

    Args:
        topic: subject for the analyst.
        run_dir: directory for control files + artifacts + resume state; empty ->
            a fresh dir under <temp>/agent-flow/.
        runtime: "opencode" (real) or "mock" (no-token demo).
        force: ignore saved state and re-run every stage.
        llm_concurrency: max concurrent LLM agent tasks (rate-limit guard).

    Returns:
        The final extractor control record.
    """
    from agent_flow.core.utils import resolve_run_dir

    logger = get_run_logger()
    wd = resolve_run_dir(run_dir, name="toy-pipeline")
    wd.mkdir(parents=True, exist_ok=True)

    report_md = wd / "analysis.md"
    report_json = wd / "analysis.json"

    logger.info("pipeline start: topic=%r runtime=%s run_dir=%s", topic, runtime, wd)

    # #7: apply the concurrency limit here, inside the flow, where the ephemeral
    # Prefect server is already running and the client can reach it.
    _apply_concurrency_limit(llm_concurrency)

    # All three stages share ONE transaction. If the extractor fails the
    # transaction rolls back: the on_rollback hook deletes the artifact recorded
    # in txn["artifact"] at the time of failure. We update that key before each
    # stage so rollback always targets the most recently written file.
    with transaction() as txn:
        # Stage 1: analyst -> writes analysis.md
        txn.set("artifact", str(report_md))
        _STAGE_TASKS["analyze"](
            stage=STAGES["analyze"],
            prompt=f"TOPIC: {topic}\nOUTPUT: {report_md}",
            run_dir=wd,
            runtime=runtime,
            force=force,
            show_events=show_events,
        )

        # Stage 2: verifier -> edits analysis.md in place (depends on stage 1)
        _STAGE_TASKS["verify"](
            stage=STAGES["verify"],
            prompt=f"REPORT: {report_md}",
            run_dir=wd,
            runtime=runtime,
            force=force,
            show_events=show_events,
        )

        # Stage 3: extractor -> writes analysis.json (depends on stage 2)
        txn.set("artifact", str(report_json))
        final = _STAGE_TASKS["extract"](
            stage=STAGES["extract"],
            prompt=f"REPORT: {report_md}\nOUTPUT: {report_json}",
            run_dir=wd,
            runtime=runtime,
            force=force,
            show_events=show_events,
        )

    logger.info("pipeline done: %s", report_json)
    return final


# CLI entry point


import typer  # noqa: E402

app = typer.Typer(add_completion=False, help="Run the analyst->verifier->extractor pipeline.")


@app.command()
def run(
    topic: str = typer.Option(..., help="subject for the analyst"),
    run_dir: str = typer.Option("", "--run-dir", help="control+artifact+state dir; empty -> a temp dir under <temp>/agent-flow/"),
    runtime: str = typer.Option("opencode", help="opencode | mock"),
    force: bool = typer.Option(False, help="ignore saved state; re-run all stages"),
    llm_concurrency: int = typer.Option(3, "--llm-concurrency", help="max concurrent LLM agent tasks"),
    show_events: bool = typer.Option(False, "--show-events", "-v", help="stream live agent events (tool calls, messages)"),
) -> None:
    """Run the pipeline. Prefect INFO logs are the diagnostics; --show-events adds
    a human-facing live view of what each agent is doing.

    Tier-2 note: this flow owns its own shape, so it keeps its own typer command
    (for the Tier-2-specific --topic/--force). But it REUSES the library's shared
    building blocks rather than re-implementing them: `build_run_config` resolves
    the generic knobs (runtime/run_dir/… via env AGENT_FLOW_*/.env too), and
    `preflight` gates the run before any agent spawns — exactly what run_cli does
    for Tier-3 flows.
    """
    import sys

    from agent_flow import preflight
    from agent_flow.cli import get_console, print_preflight_results, print_results_table
    from agent_flow.run_config import build_run_config

    console = get_console()
    # Generic run settings via the shared settings machinery (CLI > env > .env > default).
    cfg = build_run_config(
        runtime=runtime,
        run_dir=run_dir or None,
        llm_concurrency=llm_concurrency,
        show_events=True if show_events else None,
    )
    # Same pre-flight gate as run_cli: fail fast before spawning any agent.
    results = preflight.check(cfg.runtime, str(_PACKAGE_DIR))
    if preflight.fatal_failures(results):
        print_preflight_results(results, title="Pre-flight checks (run aborted)", console=console)
        sys.exit(2)

    result = pipeline(
        topic=topic,
        run_dir=cfg.run_dir,
        runtime=cfg.runtime,
        force=force,
        llm_concurrency=cfg.llm_concurrency or 3,
        show_events=cfg.show_events,
    )
    # The toy pipeline returns the final extractor control record; show it as a
    # small status line rather than a raw JSON dump.
    print_results_table(
        {"final agent": result.get("agent", "?"), "status": result.get("status", "?")}, title=f"toy pipeline — {topic}", console=console
    )


def main() -> None:
    app()


if __name__ == "__main__":
    main()
