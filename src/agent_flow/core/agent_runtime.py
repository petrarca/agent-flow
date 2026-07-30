"""`run_agent` / `arun_agent` — the Tier-1 entry point.

Run ONE agent to completion and get an `AgentResult` back. Runtime-agnostic and
backend-free: no engine, no Prefect, no DAG. Give it an agent name, a prompt, a
run directory and a runner, and it builds a neutral `AgentInvocation` and hands
it to an executor.

The supervision itself — spawning, liveness, process-group reaping, reading the
control sidecar — belongs to the executor, not here; see
`runners/subprocess_exec.py` for that machinery and its semantics.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import anyio

from agent_flow.protocol import ResultSchema
from agent_flow.runners import AgentInvocation, AgentRunner, Event
from agent_flow.runners.base import DEFAULT_IDLE_TIMEOUT_S
from agent_flow.runners.executor import AgentResult
from agent_flow.runners.subprocess_exec import SubprocessExecutor


async def arun_agent(
    *,
    agent: str,
    prompt: str,
    run_dir: Path,
    runner: AgentRunner,
    agent_dir: Path | None = None,
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
    model: str | None = None,
    instructions: str = "",
    env_extra: dict[str, str] | None = None,
    control_file: Path | None = None,
    result_schema: ResultSchema | dict | type | None = None,
    on_event: Callable[[Event], None] | None = None,
    run_instructions: str = "",
    run_context: str = "",
    node: str = "",
) -> AgentResult:
    """Run one agent as a supervised subprocess (async — the native entry point).

    Builds a neutral `AgentInvocation` from the keyword arguments and delegates to
    `SubprocessExecutor`. New async code should call this (or build an
    `AgentInvocation` and call an `AgentExecutor` directly); the sync `run_agent`
    wrapper keeps the long-standing blocking API for existing callers/tests.

    See `SubprocessExecutor` for the supervision/sidecar semantics.
    """
    inv = AgentInvocation(
        agent=agent,
        prompt=prompt,
        run_dir=run_dir,
        node=node,
        result_schema=result_schema,
        model=model,
        agent_dir=str(agent_dir) if agent_dir else "",
        instructions=instructions,
        run_instructions=run_instructions,
        run_context=run_context,
        idle_timeout_s=idle_timeout_s,
        on_event=on_event,
    )
    return await SubprocessExecutor(runner, env_extra=env_extra).run(inv, control_file=control_file)


def run_agent(
    *,
    agent: str,
    prompt: str,
    run_dir: Path,
    runner: AgentRunner,
    agent_dir: Path | None = None,
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S,
    model: str | None = None,
    instructions: str = "",
    env_extra: dict[str, str] | None = None,
    control_file: Path | None = None,
    result_schema: ResultSchema | dict | type | None = None,
    on_event: Callable[[Event], None] | None = None,
    run_instructions: str = "",
    run_context: str = "",
    node: str = "",
) -> AgentResult:
    """Run one agent as a supervised subprocess (sync back-compatible shim).

    A thin `anyio.run` wrapper over `arun_agent` — it preserves the long-standing
    blocking keyword API that Tier-1/2 callers, examples, and tests use directly.
    New async code should prefer `arun_agent` (no event-loop bridge) or building
    an `AgentInvocation` and calling an `AgentExecutor`.

    See `SubprocessExecutor` for the supervision/sidecar semantics.
    """
    return anyio.run(
        lambda: arun_agent(
            agent=agent,
            prompt=prompt,
            run_dir=run_dir,
            runner=runner,
            agent_dir=agent_dir,
            idle_timeout_s=idle_timeout_s,
            model=model,
            instructions=instructions,
            env_extra=env_extra,
            control_file=control_file,
            result_schema=result_schema,
            on_event=on_event,
            run_instructions=run_instructions,
            run_context=run_context,
            node=node,
        )
    )
