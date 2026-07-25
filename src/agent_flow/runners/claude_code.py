"""Claude Code runner — STUB with the real CLI surface as a template.

Not yet wired into the registry; documented here so the seam is concrete and a
future implementation has the verified CLI shape to fill in.
"""

from __future__ import annotations

from agent_flow.runners.base import AgentInvocation, Event


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

    def build_command(self, inv: AgentInvocation) -> list[str]:  # pragma: no cover
        cmd = ["claude", "-p", inv.prompt, "--output-format", "stream-json", "--agent", inv.agent]
        if inv.model:
            cmd += ["--model", inv.model]
        if inv.instructions:
            cmd += ["--append-system-prompt", inv.instructions]
        return cmd

    def parse_event(self, line: str) -> Event:  # pragma: no cover
        raise NotImplementedError("decode Claude Code stream-json events into Event")
