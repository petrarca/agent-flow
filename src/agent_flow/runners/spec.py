"""How a runner DECLARES itself, and how one launch is described.

`RunnerSpec` is a runner's static declaration — its transport, its mode, whether
it needs an endpoint — which `get_executor` reads to decide which executor to
pair it with. `LaunchSpec` is the concrete result of building one command.
`Check` is a single pre-flight outcome, part of the runner contract because each
runtime declares its own checks.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LaunchSpec:
    """How to launch one agent invocation — the runner's `build_command` output.

    A runner turns a (prompt-composed) `AgentInvocation` into this control
    structure. It separates the two things the executor needs but must NOT
    conflate:

      argv            the exact argv to spawn (Popen consumes it). Includes
                      whatever payload the runner chose — for a CLI runner the
                      prompt is usually the trailing positional.
      display         a human, diagnosis-safe one-line rendering of the command
                      with the (huge) prompt payload ELIDED. The runner formats
                      it because only the runner knows which parts are flags vs.
                      payload; the executor just prints it verbatim in an error.
                      A plain string, not an argv.
      capture_stderr  when True the executor captures stderr on a separate pipe
                      and passes each line to the runner's `parse_stderr_line`
                      (if implemented). False → stderr is merged into stdout
                      (the default, backward-compatible behaviour). Runners that
                      emit structured diagnostics on stderr (e.g. opencode with
                      --print-logs) set this to True so the executor can extract
                      actionable error detail without polluting the stdout parse.
    """

    argv: list[str]
    display: str
    capture_stderr: bool = False


# Execution transports. A runner declares HOW an executor talks to it:
#   "subprocess" — spawn a CLI process, stream stdout (SubprocessExecutor).
#   "http-sse"   — POST to a running daemon, stream SSE events (ServeExecutor).
# The transport decides WHICH executor a runner pairs with; the registry reads it
# from the runner's spec so executor selection never string-matches names.
TRANSPORT_SUBPROCESS = "subprocess"


TRANSPORT_HTTP_SSE = "http-sse"


# Execution modes — the human-facing axis orthogonal to the agent runtime:
#   "process" — spawn a fresh CLI process per invocation.
#   "remote"  — run against a shared, already-running daemon over the network.
MODE_PROCESS = "process"


MODE_REMOTE = "remote"


@dataclass(frozen=True)
class RunnerSpec:
    """Static, declared self-description of a runner — identity + requirements.

    Every runner returns one from `spec()`. This is the STATIC identity of a
    runner (always available, never does I/O, never fails), as opposed to
    `AgentRunnerInfo` from `info()` which is BEST-EFFORT runtime introspection
    (version/resolved-model, may be None, may fail). The registry, preflight, and
    executor selection all read this instead of parsing names or guessing from
    suffixes.

    runtime         the agent runtime family — "opencode", "goose", "crush",
                    "claude". Two runners of the same runtime (process + remote)
                    share this and their per-runtime knowledge module.
    mode            "process" | "remote" — the execution axis (MODE_*).
    transport       "subprocess" | "http-sse" — how an executor talks to this
                    runner (TRANSPORT_*). Decides which executor it pairs with.
    needs_endpoint  True when the runner requires a serve_url (a daemon endpoint).
                    Preflight and executor construction read this generically —
                    no "-remote" suffix matching anywhere.
    name            the primary registry key (e.g. "opencode", "opencode-remote").
    aliases         additional registry keys resolving to the same runner
                    (e.g. ("opencode-http",) for the remote runner).
    """

    runtime: str
    mode: str
    transport: str
    name: str
    needs_endpoint: bool = False
    aliases: tuple[str, ...] = ()


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
