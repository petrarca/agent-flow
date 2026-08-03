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
    FLOW CONTROL (read only when the node granted it):
      rerun_required  ask that an earlier step run again — see protocol.rerun
    PAYLOAD (only the application reads):
      result  free-form object with agent-specific structured data, or {}

There is deliberately NO `artifact` field: what an agent produces is expressed
in the files it was told to write (via REPORT etc. in the prompt), not reported
back through the control file.

`rerun_required` is the one flow-control lever an agent has, and it is GRANTED
per node: only a node declaring `rerun_targets` gets the field explained in its
preamble, with its legal targets named explicitly. A node without that
declaration carries none of this text — most agents never re-run anything, and
telling them all about a field their flow will ignore is noise that invites
spurious use. See `protocol.rerun` for the field's shapes and semantics.

An optional `result_schema` (a JSON-schema dict, e.g. from a Pydantic model's
`model_json_schema()`) is embedded in the preamble when supplied, to constrain
`result` — see `build_control_preamble`'s `result_schema` argument.
"""

from __future__ import annotations

import json

from agent_flow.protocol.rerun import RerunSpec


def build_control_preamble(agent: str, control_file: str, result_schema: dict | None = None, rerun: RerunSpec | None = None) -> str:
    """Return the completion-protocol instruction block to inject into a prompt.

    Args:
        agent: the agent's name (echoed into the required `agent` field).
        control_file: absolute path the agent must write its control JSON to.
        result_schema: optional JSON schema for the `result` payload. When given,
            it is embedded and the agent is told to make `result` conform to it.
        rerun: the node's re-run GRANT, when it declared `rerun_targets`. Only
            then is the re-run block appended, naming the legal targets
            explicitly. None (the default) means the agent is told nothing
            about re-running — it has not been granted the lever.
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
        # Both spellings of success exist because a verifying agent reads oddly
        # reporting plain "ok"; the engine has never distinguished them. Say so,
        # or every agent burns reasoning deciding which one is expected of it.
        '"ok" and "verified" both mean success and are treated identically — use',
        '"verified" when your task was to check work, "ok" otherwise. Only "error"',
        "changes what happens next.",
    ]

    if result_schema is not None:
        lines += [
            "",
            "### result schema (the `result` object MUST validate against this)",
            "```json",
            json.dumps(result_schema, indent=2),
            "```",
        ]

    if rerun is not None:
        lines += _rerun_block(rerun)

    return "\n".join(lines)


def _rerun_block(rerun: RerunSpec) -> list[str]:
    """The re-run instructions for a node that was GRANTED the lever.

    Shaped by arity: with a single granted target there is nothing to choose, so
    the agent answers `true`; with several it must name one. Naming the legal
    targets here is the point of the grant — it is why an agent never has to
    hardcode step names in its own definition, and why a renamed node cannot
    leave stale names behind.
    """
    lines = [
        "",
        "## Re-run request (optional)",
        "",
        "If you find an earlier step's output wrong or incomplete, you may ask for it",
        'to run again by adding "rerun_required" to the control file above.',
        "",
        # Say WHERE, not just what: shown as a bare fragment, a reader cannot tell
        # a top-level key from one nested in `result` — and only the top-level one
        # is read, so a wrong guess fails silently.
        'Put it at the TOP LEVEL, as a sibling of "status" — never inside "result":',
        "",
    ]
    sole = rerun.sole_target
    if sole:
        lines += [
            "  {",
            '    "status": "…", "agent": "…",',
            '    "rerun_required": <one of the two values below>',
            "  }",
            "",
            # Two separate facts, deliberately in separate sentences: WHICH step a
            # request means, and that there is no key to express it with. Stated
            # together ("the only step is X — never name it") they read as a
            # contradiction, and an agent then wonders whether some `step` key is
            # tolerated after all.
            f"A request here always means one step: {sole}.",
            "",
            # Both values shown TOGETHER: split across the block ("the form above
            # and the one below") they read as vague cross-references, and the
            # first one alone looks like the only legal value.
            "There is no target key. These are the only two accepted values:",
            "",
            "    true",
            f'    {{ "instruction": "what {sole} must fix or redo" }}',
        ]
    else:
        lines += [
            "  {",
            '    "status": "…", "agent": "…",',
            '    "rerun_required": { "target": "<step>", "instruction": "what it must fix or redo" }',
            "  }",
            "",
            "The step you name must be exactly one of these:",
            "",
        ]
        # A GROUP name is opaque on its own — spell out what it covers, so the
        # choice between a group and one of its members is an informed one.
        lines += [f"  - {t.name}" + (f"  (runs: {', '.join(t.members)})" if t.members else "") for t in rerun.targets]
        lines += [
            "",
            "Name the EARLIEST affected step: the flow jumps back to it and re-runs",
            "everything downstream, so naming a later step as well is neither needed nor",
            "correct.",
        ]
    lines += [
        "",
        "The instruction is optional and is handed to that step verbatim on its next",
        'run. Requesting a re-run is never required: omit "rerun_required" entirely',
        "when nothing needs to run again, which is the normal case.",
    ]
    return lines
