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

from typing import Any, cast

from agent_flow.runners.base import AgentRunner, RunnerBase
from agent_flow.runners.claude_code import ClaudeCodeRunner
from agent_flow.runners.events import Event
from agent_flow.runners.executor import AgentExecutor
from agent_flow.runners.inprocess import AgentImpl, InProcessExecutor
from agent_flow.runners.invocation import DEFAULT_IDLE_TIMEOUT_S, AgentInvocation, AgentRunnerInfo, compose_prompt
from agent_flow.runners.mock_exec import MockAgent, MockAgentContext, MockExecutor
from agent_flow.runners.opencode import OpenCodeRunner
from agent_flow.runners.prompt import PromptParts, render_prompt
from agent_flow.runners.spec import (
    MODE_PROCESS,
    MODE_REMOTE,
    TRANSPORT_HTTP_SSE,
    TRANSPORT_SUBPROCESS,
    Check,
    LaunchSpec,
    RunnerSpec,
)
from agent_flow.runners.subprocess_exec import SubprocessExecutor

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


def probe_agent_dir(runtime: str) -> str | None:
    """Ask a runtime's runner to locate its agent-definitions dir (comfort feature).

    Called only when NO explicit agent_dir was set anywhere — the LOWEST slot in
    the precedence chain, above the "none configured" preflight error. The runner
    owns the convention (opencode probes for `.opencode/` in cwd + ancestors); the
    library hardcodes no marker. A runner that does not implement the OPTIONAL
    `default_agent_dir` (a remote one) yields None, so the requirement surfaces at
    preflight. Best-effort: an unknown runtime or a filesystem error yields None,
    never raises.
    """
    try:
        runner = get_runner(runtime)
    except ValueError:
        return None
    probe = getattr(runner, "default_agent_dir", None)
    if not callable(probe):
        return None
    try:
        return probe()
    except OSError:
        return None


def get_executor(name: str, *, serve_url: str = "", options: dict[str, Any] | None = None) -> AgentExecutor:
    """Resolve an AgentExecutor for a runtime name (the "runtime" run param).

    The runner's SPEC decides the executor: a subprocess-transport runner is
    wrapped in SubprocessExecutor; an http-sse runner (a `-remote` runtime) is
    wrapped in ServeExecutor with the endpoint. `serve_url` is required when the
    runner's spec sets `needs_endpoint`. Mock is NOT a runtime — it is the
    `--mock-agents` mode, routed to MockExecutor in node_builder, never here.

    `options` is the runtime-SPECIFIC bag (RunConfig `options:` merged with a
    node's own). Known keys are read from it here; `serve_url` remains an explicit
    kwarg for direct Tier-1 callers and, when set, wins over `options["serve_url"]`.
    """
    options = options or {}
    # The explicit kwarg wins (a direct caller's intent); else fall to the bag.
    serve_url = serve_url or str(options.get("serve_url") or "")
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
    # The registry is heterogeneous (one entry per runtime, any transport); the
    # `transport` tag on the runner's own spec is what discriminates them. Having
    # ruled out the remote transports above, this runner IS a subprocess runner
    # (i.e. also satisfies AgentRunner: build_command + name) — a fact carried by
    # the spec, not by the static type, hence the cast.
    return SubprocessExecutor(cast("AgentRunner", runner))


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
    "probe_agent_dir",
    "AgentRunnerInfo",
    "Check",
    "AgentInvocation",
    "AgentExecutor",
    "InProcessExecutor",
    "AgentImpl",
    "MockExecutor",
    "MockAgentContext",
    "MockAgent",
    "Event",
    "compose_prompt",
    "PromptParts",
    "render_prompt",
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
