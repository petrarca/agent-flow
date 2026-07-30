"""Flow-control gates — the orchestration-layer decision point after an agent run.

The engine (`run_agent`) supervises exactly ONE subprocess and returns an
`AgentResult`; it knows nothing about nodes, artifacts, or the DAG. Everything
that is a FLOW decision — "the report file is missing, re-run this node",
"resume the flow at an earlier node", "this blocking node failed, stop" — is
expressed here, as a per-node GATE. The gate is the CONSUMER's optional hook.

A gate is any callable `(GateContext) -> Directive`. The engine invokes it after
the agent completes and acts on the returned directive. A node with NO gate
behaves as if the gate returned `Continue()` — the default, absent-means-continue
rule.

This is the seam that keeps domain knowledge (what a report is, when a re-run is
needed) out of the engine: a gate is free to stat files, read the control dict,
inspect telemetry — whatever it needs — and translate that into one of four
directives.

Directives:

    Continue()          proceed to the next node (also the default / gate absent)
    Restart()           re-run THIS node's agent (bounded by max_cycles)
    GoTo(node)          resume the flow at a named node (re-run loops, jump-back)
    Stop(reason)        abort the whole pipeline (e.g. blocking-criticality failure)

Restart and GoTo carry an optional one-time `instruction` (plain-text prompt
guidance for the re-run) — distinct from Stop's `reason` (a backward-looking
abort message for the operator/log). See each directive's docstring.

Module map:
  types.py    the Directive vocabulary (Continue / Restart / GoTo / Stop) and
              GateContext — what a gate is handed and what it may return
  signals.py  building blocks: did the agent write the file, does its envelope
              name nodes to re-run
  builtin.py  the gates that ship: require_file, rerun_on_signal, rerun_on_named
"""

from __future__ import annotations

from agent_flow.gates.builtin import require_file, rerun_on_named, rerun_on_signal
from agent_flow.gates.signals import produced, rerun_targets
from agent_flow.gates.types import Continue, Directive, Gate, GateContext, GoTo, Restart, Stop

__all__ = [
    "Continue",
    "Directive",
    "Gate",
    "GateContext",
    "GoTo",
    "Restart",
    "Stop",
    "produced",
    "require_file",
    "rerun_on_named",
    "rerun_on_signal",
    "rerun_targets",
]
