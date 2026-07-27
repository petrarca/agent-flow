"""flowdef — the declarative, serializable pipeline definition (the consumer surface).

A pipeline is a `FlowDef` of `NodeDef`s: pure pydantic DATA (no callables), so it
serializes to JSON/YAML, validates before any run, and round-trips through a
designer. `compile_flow(flow_def, registry)` turns it into the internal runtime
`Node` list the engine executes — resolving names (gate/export/run/schema) via a
`FlowRegistry`. Node/agent_node/build_flow remain the lower runtime layer.
"""

from __future__ import annotations

from agent_flow.flowdef.compile import arun_flow, compile_flow, run_flow
from agent_flow.flowdef.models import FlowDef, NodeDef

__all__ = ["FlowDef", "NodeDef", "arun_flow", "compile_flow", "run_flow"]
