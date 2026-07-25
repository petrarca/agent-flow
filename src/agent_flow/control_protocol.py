"""The control-file protocol — the engine's completion contract with an agent.

Every supervised agent signals completion the same way: it writes a JSON
control file. That protocol (where to write, what shape) is the ENGINE's
concern, not each agent's domain knowledge — so the library injects it into the
prompt instead of every agent .md restating it.

`build_control_preamble` returns the standard instruction block. `run_agent`
prepends it automatically whenever a `control_file` is set, so agent .md files
carry only their DOMAIN instructions ("analyze X, write a report to REPORT") and
never the control-JSON boilerplate.

The control file shape:

    ENVELOPE (the engine reads):
      status  "ok" | "verified" | "error"   (required)
      agent   the agent's own name
      reason  short explanation, for error status
      rerun_required  optional list of NODE names to re-run (a flow-control
                      signal a gate consumes)
    PAYLOAD (only gates/callers read):
      result  free-form object with agent-specific structured data, or {}

There is deliberately NO `artifact` field: what an agent produces is expressed
in the files it was told to write (via REPORT etc. in the prompt), not reported
back through the control file.

`rerun_required` is how an agent (e.g. one that checks earlier work) asks the
orchestrator to re-run an EARLIER step. It is genuinely optional and most agents
never use it — but it must be mentioned in the injected preamble, because a real
agent has no other way to learn this field exists (it cannot see the engine's
`gates.rerun_on_signal`). Consumers that rely on it should also say so plainly in
that agent's own instructions (its `.md`), since a general reminder in the shared
preamble is not a substitute for the agent knowing WHEN to use it for its
specific task.

An optional `result_schema` (a JSON-schema dict, e.g. from a Pydantic model's
`model_json_schema()`) is embedded in the preamble when supplied, to constrain
`result` — see `build_control_preamble`'s `result_schema` argument.
"""

from __future__ import annotations

import json


def build_control_preamble(agent: str, control_file: str, result_schema: dict | None = None) -> str:
    """Return the completion-protocol instruction block to inject into a prompt.

    Args:
        agent: the agent's name (echoed into the required `agent` field).
        control_file: absolute path the agent must write its control JSON to.
        result_schema: optional JSON schema for the `result` payload. When given,
            it is embedded and the agent is told to make `result` conform to it.
    """
    result_line = '    "result": {}              // optional: agent-specific structured data'
    if result_schema is not None:
        result_line = '    "result": { ... }        // MUST conform to the JSON schema below'

    lines = [
        "## Completion protocol (required)",
        "",
        # A KEY: value line so both LLM agents and the mock agent (which parses
        # `CONTROL_FILE:` from the prompt) learn where to write.
        f"CONTROL_FILE: {control_file}",
        "",
        "When you finish, write a JSON control file to the CONTROL_FILE path above.",
        "Use the Write tool (write the file — do NOT print the JSON to stdout).",
        "Write it as your FINAL action, exactly one JSON object:",
        "",
        "  {",
        '    "status": "ok",            // "ok" | "verified" | "error"',
        f'    "agent": "{agent}",',
        '    "reason": "",              // short explanation, only if status is "error"',
        result_line,
        "  }",
        "",
        'If you cannot complete the task, write status "error" with a "reason".',
        "",
        'Optional field "rerun_required": a JSON array of step names that must be',
        "re-run before this work is trusted (e.g. if you find the input from an",
        "earlier step to be wrong or incomplete). Include it ONLY if your specific",
        "instructions below tell you to check for this and name which step(s) it",
        "applies to — most agents never need it and should omit it entirely.",
    ]

    if result_schema is not None:
        lines += [
            "",
            "### result schema (the `result` object MUST validate against this)",
            "```json",
            json.dumps(result_schema, indent=2),
            "```",
        ]

    return "\n".join(lines)
