"""Claude Code runner — STUB with the real CLI surface as a template.

Not yet wired into the registry; documented here so the seam is concrete and a
future implementation has the verified CLI shape to fill in.
"""

from __future__ import annotations

import shlex

from agent_flow.runners.base import MODE_PROCESS, TRANSPORT_SUBPROCESS, AgentInvocation, Event, LaunchSpec, RunnerSpec


class ClaudeCodeRunner:
    """STUB with the REAL Claude Code CLI surface (verified) as a template.

    Claude Code headless mode:
      - one-shot:            claude -p "<prompt>"
      - streaming events:    --output-format stream-json  (newline-delimited JSON;
                             final event carries token usage + USD cost)
      - named agent:         --agent <name>   (Claude Code DOES support this)
      - model:               --model <name>   (+ optional --fallback-model)
      - inject instructions: --append-system-prompt "<inv.instructions>"

    Unlike opencode (whose identity lives in the agent .md), Claude Code takes
    the agent's standing instructions via --append-system-prompt — which is why
    AgentInvocation carries `instructions` separately from the work-order prompt.

    parse_event would decode Claude's stream-json event shape into an Event
    (tokens/cost from the final usage event; is_terminal on the finish event).
    Left unimplemented until we actually run Claude Code.

    info() note: Claude Code has NO CLI subcommand that prints resolved config
    (opencode's `debug config` has no equivalent). Resolution is SDK-only
    (`resolveSettings({cwd})`, alpha). So info() should report version/available
    from `claude --version` and leave resolved_model=None, tools=[] (or call the
    Agent SDK if a dependency on it is later accepted). Config resolves relative
    to cwd here too (.claude/settings.local.json > project > user), with a
    managed/MDM policy tier ABOVE CLI args — hence the model-precedence caveat in
    the module header.
    """

    name = "claude"

    def spec(self) -> RunnerSpec:
        """Static identity: claude runtime, process mode, subprocess transport.

        Claude Code has no serve/daemon mode (headless is subprocess-per-call or
        an in-process SDK), so there is no remote variant — needs_endpoint stays
        False and no http-sse runner exists for it.
        """
        return RunnerSpec(
            runtime="claude",
            mode=MODE_PROCESS,
            transport=TRANSPORT_SUBPROCESS,
            name=self.name,
            needs_endpoint=False,
        )

    def build_command(self, inv: AgentInvocation) -> LaunchSpec:  # pragma: no cover
        # NOTE: unlike opencode, Claude puts the prompt right after `-p` (NOT the
        # trailing positional). This is exactly why display-elision belongs to the
        # runner: only it knows where its own prompt sits.
        argv = ["claude", "-p", inv.prompt, "--output-format", "stream-json", "--agent", inv.agent]
        if inv.model:
            argv += ["--model", inv.model]
        if inv.instructions:
            argv += ["--append-system-prompt", inv.instructions]
        # display: the same argv with the prompt (and any long system prompt)
        # replaced by short markers.
        shown = [
            (
                f"<prompt: {len(a)} chars>"
                if a == inv.prompt
                else f"<instructions: {len(a)} chars>"
                if a == inv.instructions and inv.instructions
                else a
            )
            for a in argv
        ]
        return LaunchSpec(argv=argv, display=shlex.join(shown))

    def parse_event(self, line: str) -> Event:  # pragma: no cover
        raise NotImplementedError("decode Claude Code stream-json events into Event")
