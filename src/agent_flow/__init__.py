"""agent-flow — deterministic orchestration of coding-agent pipelines.

A small library that replaces LLM "orchestrator agents" with a deterministic
engine: declare a pipeline as typed data and the library runs it, supervising
each agent as a subprocess (liveness, kill, sidecar status), with parallelism,
bounded re-runs, criticality, and telemetry. The execution backend (Prefect) and
the agent runtime (opencode, Claude Code, …) are both pluggable.

Public API:

    from agent_flow import (
        # Tier 1: one supervised agent (the primitive)
        run_agent, AgentResult,
        AgentTimeoutError, AgentContentFailedError, AgentCrashError,
        # runners (swappable agent runtimes)
        AgentRunner, AgentInvocation, Event,
        OpenCodeRunner, MockRunner, get_runner,
        # Tier 3: declare Nodes -> build_flow (the batteries)
        Node, NodeOutcome, RunContext, build_flow, NodeBlocked,
        agent_node, control_path,
        # flow-control gates — the consumer's optional hook
        Gate, GateContext, Directive,
        Continue, Restart, GoTo, Stop,
        require_file, rerun_on_signal,
        # typed agent output (opt-in, Pydantic optional)
        ResultSchema, JsonSchema, ValidationOutcome, coerce_schema,
        # the injected control-file protocol
        build_control_preamble,
        # CLI rendering helpers (optional; needs the `cli` extra)
        event_printer, get_console, print_results_table,
        # file-based signals (gate building blocks)
        produced, rerun_from_sidecar,
        # environment
        load_env,
    )

See examples/ for how to build a pipeline on top of this library, and
docs/design/orchestrator/ for the full design.
"""

from agent_flow.agent_runtime import (
    AgentContentFailedError,
    AgentCrashError,
    AgentResult,
    AgentTimeoutError,
    run_agent,
)
from agent_flow.batteries import agent_node, control_path
from agent_flow.cli import NodeProgressPrinter, event_printer, get_console, print_preflight_results, print_results_table, run_cli
from agent_flow.context import read_context_blocks
from agent_flow.control_protocol import build_control_preamble
from agent_flow.engine import (
    Node,
    NodeBlocked,
    NodeOutcome,
    RunContext,
    build_flow,
    interpret,
    plan_groups,
)
from agent_flow.env import load_env
from agent_flow.gates import (
    Continue,
    Directive,
    Gate,
    GateContext,
    GoTo,
    Restart,
    Stop,
    require_file,
    rerun_on_signal,
)
from agent_flow.preflight import Check, check, fatal_failures
from agent_flow.report_signals import produced, rerun_from_sidecar
from agent_flow.run_config import (
    RunConfig,
    build_run_config,
    clear_settings,
    get_settings,
    init_settings,
    parse_params,
)
from agent_flow.runners import (
    AgentInvocation,
    AgentRunner,
    Event,
    MockRunner,
    OpenCodeRunner,
    get_runner,
)
from agent_flow.schema import JsonSchema, ResultSchema, ValidationOutcome, coerce_schema
from agent_flow.schema_pydantic import PydanticSchema
from agent_flow.utils import default_temp_base, resolve_run_dir

__all__ = [
    # agent execution
    "run_agent",
    "AgentResult",
    "AgentTimeoutError",
    "AgentContentFailedError",
    "AgentCrashError",
    # runners
    "AgentRunner",
    "AgentInvocation",
    "Event",
    "OpenCodeRunner",
    "MockRunner",
    "get_runner",
    # control-file protocol (injected into agent prompts)
    "build_control_preamble",
    # CLI (optional; needs the `cli` extra)
    "event_printer",
    "get_console",
    "print_results_table",
    "print_preflight_results",
    "NodeProgressPrinter",
    "run_cli",
    "RunConfig",
    "build_run_config",
    "get_settings",
    "init_settings",
    "clear_settings",
    "parse_params",
    "resolve_run_dir",
    "default_temp_base",
    # result-schema seam (typed agent output; Pydantic optional)
    "ResultSchema",
    "JsonSchema",
    "PydanticSchema",
    "ValidationOutcome",
    "coerce_schema",
    # declaration-driven engine (Layer 3)
    "Node",
    "NodeBlocked",
    "NodeOutcome",
    "RunContext",
    "build_flow",
    "plan_groups",
    "interpret",
    # batteries: one-call node for the common "run one agent" case
    "agent_node",
    "control_path",
    # context ingestion (read files -> prompt content)
    "read_context_blocks",
    # flow-control gates
    "Gate",
    "GateContext",
    "Directive",
    "Continue",
    "Restart",
    "GoTo",
    "Stop",
    # ready-made gates (optional conveniences)
    "require_file",
    "rerun_on_signal",
    # signals
    "produced",
    "rerun_from_sidecar",
    # pre-flight checks
    "Check",
    "check",
    "fatal_failures",
    # env
    "load_env",
]
