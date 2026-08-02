"""File-based signals — building blocks a GATE uses to decide flow control.

These are NOT consulted by the engine (`run_agent` keys success solely on the
control verdict). They are helpers a pipeline's gate calls to inspect what an
agent produced and translate that into a directive (Continue / Restart / GoTo /
Stop). This keeps artifact knowledge in the gate, out of the engine.

Signals:

  1. produced(path) -> did the agent actually write a non-empty file there? A
     gate can use this to veto an otherwise-ok run (e.g. return Restart() when
     the control says ok but nothing landed). This is a genuine FILESYSTEM check
     about the agent's WORK PRODUCT — the artifact the agent was told to write.
     The library attaches no meaning to what that artifact IS; the gate does.

  2. rerun_targets(control) -> does the agent's CONTROL ENVELOPE name any NODES
     in its `rerun_required` field? Operates on the ALREADY-HARVESTED envelope
     dict (`ctx.result`), NOT a file — by the time a gate runs, the executor has
     already harvested the verdict however it came back (sidecar / structured
     output / file API). So the gate reads the dict it was handed, never a file.
     See gates.rerun_on_signal / rerun_on_named.

  3. read_field(ctx, name) -> the value of a result FIELD, read the same robust
     way regardless of how the pipeline shaped its result: the validated typed
     object (`ctx.obj`) when a `result_schema` was set, else the raw envelope
     dict (`ctx.result`), else that envelope's `result` payload. A building block
     for any gate that decides from a structured field (see gates.stop_if).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any


def produced(path: Path) -> bool:
    """True if the agent wrote a non-empty file at `path`."""
    return path.exists() and path.stat().st_size > 0


# Sentinel distinguishing "field absent" from a field whose value is None.
_MISSING = object()


def read_field(ctx: Any, name: str, default: Any = None) -> Any:
    """Read result FIELD `name` from a GateContext, typed-object-or-dict agnostic.

    Resolution order — the first place the field is present wins:
      1. `ctx.obj`     the VALIDATED typed result object (a pydantic instance)
                       when the node declared a `result_schema` — attribute lookup.
      2. `ctx.result`  the raw control envelope dict — top-level key lookup
                       (status/reason/… and any field the agent put at top level).
      3. `ctx.result["result"]` the free-form `result` payload dict — key lookup,
                       where an untyped pipeline usually puts its structured data.

    A gate should not care whether the consumer typed its result; this reads the
    same field either way. Returns `default` (None) when the field is nowhere.
    """
    obj = getattr(ctx, "obj", None)
    if obj is not None:
        val = getattr(obj, name, _MISSING)
        if val is not _MISSING:
            return val
    result = getattr(ctx, "result", None)
    if isinstance(result, dict):
        if name in result:
            return result[name]
        payload = result.get("result")
        if isinstance(payload, dict) and name in payload:
            return payload[name]
    return default


def rerun_targets(control: dict | None) -> list[str]:
    """Return the NODE names in a control ENVELOPE's `rerun_required` field.

    Operates on the harvested control dict (what the engine hands a gate as
    `ctx.result`) — NOT a file. The verdict is already in memory by the time a
    gate runs, regardless of HOW it was harvested (subprocess sidecar, remote
    structured output, remote file API); the gate reads the dict, never re-reads
    a file or reconstructs a path.

    `rerun_required` is a flow-control signal, so it lives in the envelope
    (alongside status/agent/reason), NOT in the free-form `result` payload. It
    names the NODE(s) to re-run — nodes are the unit of the DAG; the agent behind
    a node is an implementation detail (for simplicity a node is often named after
    its agent, but the SIGNAL is a node name):

        {"status": "verified", "agent": "domain-verifier",
         "rerun_required": ["domain"], "result": {...}}

    Returns the list (empty when no re-run needed or the field is absent/invalid).
    """
    if not isinstance(control, dict):
        return []
    val = control.get("rerun_required")
    if isinstance(val, list):
        return [str(x) for x in val]
    if isinstance(val, str) and val.strip():
        return [val.strip()]
    return []
