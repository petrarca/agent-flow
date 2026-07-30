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
siblings directly — that is not a boundary crossing.

The control protocol and the result-schema types are NOT re-exported here: they
live in `agent_flow.protocol`, below both core and runners, and are imported
from there. Each name has exactly one home.
"""

from __future__ import annotations

from agent_flow.core.agent_runtime import DEFAULT_IDLE_TIMEOUT_S, AgentResult, arun_agent, run_agent
from agent_flow.core.context import read_context_blocks
from agent_flow.core.env import load_env
from agent_flow.runners.executor import AgentContentFailedError, AgentCrashError, AgentTimeoutError

__all__ = [
    # one supervised agent
    "run_agent",
    "arun_agent",
    "AgentResult",
    "AgentTimeoutError",
    "AgentContentFailedError",
    "AgentCrashError",
    "DEFAULT_IDLE_TIMEOUT_S",
    # context ingestion (files -> prompt content)
    "read_context_blocks",
    # environment
    "load_env",
]
