"""Agent runners + the execution seam.

Two seams live under this package:

  - `AgentExecutor` (ABC, `executor.py`) — the high-level "run one invocation ->
    AgentResult" seam. Concrete kinds: `SubprocessExecutor` (in
    `core/agent_runtime.py`), `ServeExecutor` (`serve_executor.py`),
    `InProcessExecutor` (`inprocess.py`), and `MockExecutor` (`mock_exec.py`). It
    owns the shared result-assembly tail and status policy (`assemble_result` /
    `check_content_status`).
  - `RunnerBase` (Protocol, `base.py`) — the LOW-LEVEL wire adapter every runner
    shares: `spec()` (static identity) + `parse_event` (event vocabulary). Two
    transport sub-protocols extend it: `AgentRunner` adds `build_command`
    (subprocess) and `RemoteRunner` adds `build_request` (http-sse). The neutral
    contract (`Event` / `AgentInvocation` / `RunnerSpec` / `AgentRunnerInfo`)
    lives in `base.py`; each runtime is a sibling module (`opencode.py`,
    `claude_code.py`).

Executor selection is driven by the runner's `RunnerSpec.transport`: a
subprocess-transport runner is wrapped in `SubprocessExecutor`, an http-sse
runner in `ServeExecutor`. Runners REGISTER THEMSELVES via `register()` keyed by
their spec's name + aliases — adding a runtime is one `register()` line, no
separate name map to keep in sync.

Runners are `typing.Protocol`s (structural), not ABCs: they match by SHAPE and
do not inherit — the wire methods are entirely runtime-specific. `AgentExecutor`
IS an ABC because its kinds share real logic (result assembly + status policy).
Mock and inproc are NOT runtimes: they are executor MODES selected in
`node_builder` before runtime resolution, never in the registry.
"""

from __future__ import annotations

from agent_flow.runners.base import (
    DEFAULT_IDLE_TIMEOUT_S,
    MODE_PROCESS,
    MODE_REMOTE,
    TRANSPORT_HTTP_SSE,
    TRANSPORT_SUBPROCESS,
    AgentInvocation,
    AgentRunner,
    AgentRunnerInfo,
    Event,
    LaunchSpec,
    RunnerBase,
    RunnerSpec,
    compose_prompt,
)
from agent_flow.runners.claude_code import ClaudeCodeRunner
from agent_flow.runners.executor import AgentExecutor
from agent_flow.runners.inprocess import AgentImpl, InProcessExecutor
from agent_flow.runners.mock_exec import MockAgent, MockAgentContext, MockExecutor
from agent_flow.runners.opencode import OpenCodeRunner

# The runner registry — name/alias -> runner instance. Runners REGISTER THEMSELVES
# via `register()` (called at the bottom of this module), keyed by their spec's
# `name` plus any `aliases`. `mock` and `inproc` are NOT runtimes — they are
# executor MODES selected in node_builder before runtime resolution, so they never
# appear here. The registry only holds REAL external-runtime runners.
_REGISTRY: dict[str, RunnerBase] = {}


def register(runner: RunnerBase) -> RunnerBase:
    """Register a runner under its spec's name + aliases. Returns the runner.

    Idempotent per name: a duplicate name raises, so two runners can never claim
    the same key. The single source of truth for a runner's keys is its RunnerSpec
    (name + aliases) — the registry never invents names.
    """
    spec = runner.spec()
    for key in (spec.name, *spec.aliases):
        if key in _REGISTRY:
            raise ValueError(f"runner key {key!r} already registered (by {_REGISTRY[key].spec().name!r})")
        _REGISTRY[key] = runner
    return runner


def get_runner(name: str) -> RunnerBase:
    """Resolve a runner by name or alias — e.g. "opencode" / "opencode-remote"."""
    try:
        return _REGISTRY[name]
    except KeyError:
        raise ValueError(f"unknown runner {name!r} (available: {sorted(_REGISTRY)})") from None


def runner_specs() -> list[RunnerSpec]:
    """Every DISTINCT registered runner's spec (deduped — aliases collapse)."""
    seen: dict[int, RunnerSpec] = {}
    for runner in _REGISTRY.values():
        seen.setdefault(id(runner), runner.spec())
    return list(seen.values())


def get_executor(name: str, *, serve_url: str = "") -> AgentExecutor:
    """Resolve an AgentExecutor for a runtime name (the "runtime" run param).

    The runner's SPEC decides the executor: a subprocess-transport runner is
    wrapped in SubprocessExecutor; an http-sse runner (a `-remote` runtime) is
    wrapped in ServeExecutor with the endpoint. `serve_url` is required when the
    runner's spec sets `needs_endpoint`. Mock is NOT a runtime — it is the
    `--mock-agents` mode, routed to MockExecutor in node_builder, never here.
    """
    # Imported lazily: SubprocessExecutor lives in core.agent_runtime (next to the
    # subprocess machinery), which imports this package — avoid an import cycle.
    from agent_flow.core.agent_runtime import SubprocessExecutor

    try:
        runner = get_runner(name)
    except ValueError:
        raise ValueError(f"unknown runtime {name!r} (available: {sorted(_REGISTRY)})") from None
    spec = runner.spec()
    if spec.needs_endpoint and not serve_url:
        raise ValueError(f"runtime {name!r} requires a serve_url (remote runtime, transport={spec.transport!r})")
    if spec.transport == TRANSPORT_HTTP_SSE:
        # ServeExecutor lives with the serve machinery; lazy import mirrors the
        # SubprocessExecutor import above and avoids a cycle at module load.
        from agent_flow.runners.serve_executor import ServeExecutor

        return ServeExecutor(runner, url=serve_url)
    return SubprocessExecutor(runner)


# --- self-registration ------------------------------------------------------
# Each real runtime registers itself here. Adding a runtime = implement the
# runner (with a spec()) and add ONE register() line — no separate name map to
# keep in sync. `claude` is a valid runner but stays UNregistered until its
# parse_event is implemented (build_command is a stub).
register(OpenCodeRunner())
# register(ClaudeCodeRunner())   # enable when parse_event is implemented
# register(OpenCodeRemoteRunner())  # enable when the http-sse runner lands


__all__ = [
    "AgentRunner",
    "RunnerBase",
    "RunnerSpec",
    "AgentRunnerInfo",
    "AgentInvocation",
    "AgentExecutor",
    "InProcessExecutor",
    "AgentImpl",
    "MockExecutor",
    "MockAgentContext",
    "MockAgent",
    "Event",
    "compose_prompt",
    "LaunchSpec",
    "DEFAULT_IDLE_TIMEOUT_S",
    "MODE_PROCESS",
    "MODE_REMOTE",
    "TRANSPORT_SUBPROCESS",
    "TRANSPORT_HTTP_SSE",
    "OpenCodeRunner",
    "ClaudeCodeRunner",
    "register",
    "runner_specs",
    "get_runner",
    "get_executor",
]
