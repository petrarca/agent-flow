"""File-based signals — building blocks a GATE uses to decide flow control.

These are NOT consulted by the engine (`run_agent` keys success solely on the
control sidecar). They are helpers a pipeline's gate calls to inspect what an
agent wrote on disk (or reported in its sidecar) and translate that into a
directive (Continue / Restart / GoTo / Stop). This keeps artifact knowledge in
the gate, out of the engine.

Signals:

  1. produced(report_path) -> did the agent actually write a non-empty report
     file? A gate can use this to veto an otherwise-ok run (e.g. return
     Restart() when the control says ok but no report landed).

  2. rerun_from_sidecar(control_file) -> does the agent's CONTROL SIDECAR name
     any agents in its `rerun_required` field? This is the actual mechanism (a
     JSON field in the envelope, not a markdown block in the report) — see
     control_protocol.py and gates.rerun_on_signal.
"""

from __future__ import annotations

import json
from pathlib import Path


def produced(report_path: Path) -> bool:
    """True if the agent wrote a non-empty report file."""
    return report_path.exists() and report_path.stat().st_size > 0


def rerun_from_sidecar(control_file: Path) -> list[str]:
    """Return agents named in the sidecar ENVELOPE's `rerun_required` field.

    `rerun_required` is a flow-control signal, so it lives in the envelope
    (alongside status/agent/reason), NOT in the free-form `result` payload:

        {"status": "verified", "agent": "domain-verifier",
         "rerun_required": ["domain-analyst"], "result": {...}}

    Returns the list (empty when no re-run needed or the file is absent/invalid).
    """
    if not control_file.exists():
        return []
    try:
        data = json.loads(control_file.read_text())
    except json.JSONDecodeError, OSError:
        return []
    val = data.get("rerun_required")
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []
