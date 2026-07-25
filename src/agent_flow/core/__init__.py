"""agent-flow core — the Tier-1 primitives, GUARANTEED backend-free.

This package holds the pieces that run and validate ONE supervised agent, with
no dependency on the execution backend (Prefect and the FlowBackend seam live
elsewhere). It is the layer a foreign orchestrator (Airflow, Temporal, a bespoke
loop) imports to reuse agent-flow's supervision without pulling in the DAG
engine or any flow backend.

Layer order (who may depend on whom):

    utils  (pure, dependency-free helpers: template/run-dir; usable by ANYONE,
            including runners — the bottom leaf)
        <  runners  (pure strategy: build_command / parse_event)
        <  core  (run_agent uses a runner; validates output; reads context)
        <  engine / gates / node_builder
        <  backends  (the execution seam)
        <  cli

`utils` deliberately lives at the top level, not inside core, so lower layers
(e.g. runners) can use its pure helpers without depending on core.

So `core` depends on `runners` (run_agent needs a runner) and on nothing above
it. It never imports engine, backends, or cli — that one-directional rule keeps
core importable in isolation and is guarded by the prefect-isolation test.

Cross-package boundary: other packages import core symbols FROM HERE (the public
surface below), not from core's submodules. Modules WITHIN core import their
siblings directly (e.g. agent_runtime imports core.schema) — that is not a
boundary crossing and avoids import cycles.
"""

from __future__ import annotations

from agent_flow.core.agent_runtime import (
    DEFAULT_IDLE_TIMEOUT_S,
    AgentContentFailedError,
    AgentCrashError,
    AgentResult,
    AgentTimeoutError,
    run_agent,
)
from agent_flow.core.context import read_context_blocks
from agent_flow.core.control_protocol import build_control_preamble
from agent_flow.core.env import load_env
from agent_flow.core.report_signals import produced, rerun_from_sidecar
from agent_flow.core.schema import JsonSchema, ResultSchema, ValidationOutcome, coerce_schema
from agent_flow.core.schema_pydantic import PydanticSchema

__all__ = [
    # one supervised agent
    "run_agent",
    "AgentResult",
    "AgentTimeoutError",
    "AgentContentFailedError",
    "AgentCrashError",
    "DEFAULT_IDLE_TIMEOUT_S",
    # injected control-file protocol
    "build_control_preamble",
    # context ingestion (files -> prompt content)
    "read_context_blocks",
    # typed agent output (opt-in)
    "ResultSchema",
    "JsonSchema",
    "PydanticSchema",
    "ValidationOutcome",
    "coerce_schema",
    # file-based signals (gate building blocks)
    "produced",
    "rerun_from_sidecar",
    # environment
    "load_env",
]
