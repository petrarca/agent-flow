"""agent-flow — deterministic orchestration of coding-agent pipelines.

A small library that replaces LLM "orchestrator agents" with a deterministic
engine: declare a pipeline as typed data and the library runs it, supervising
each agent as a subprocess (liveness, kill, sidecar status), with parallelism,
bounded re-runs, criticality, and telemetry. The execution backend (Prefect) and
the agent runtime (opencode, Claude Code, …) are both pluggable.

Public API (the authoritative list is `__all__` below):

    from agent_flow import (
        # Tier 1: one supervised agent (the primitive)
        run_agent, AgentResult,
        AgentTimeoutError, AgentContentFailedError, AgentCrashError,
        # runners (swappable agent runtimes)
        AgentRunner, AgentInvocation, Event,
        OpenCodeRunner, MockRunner, get_runner,
        # Tier 3: declare Nodes -> build_flow (the DAG engine)
        Node, NodeOutcome, RunContext, build_flow, NodeBlocked,
        plan_groups, interpret,
        # batteries: one-call node for the common "run one agent" case
        agent_node, control_path,
        # flow-control gates — the consumer's optional hook
        Gate, GateContext, Directive,
        Continue, Restart, GoTo, Stop,
        require_file, rerun_on_signal, rerun_on_named,
        # file-based signals (gate building blocks)
        produced, rerun_from_sidecar,
        # typed agent output (opt-in)
        ResultSchema, JsonSchema, PydanticSchema, ValidationOutcome, coerce_schema,
        # the injected control-file protocol
        build_control_preamble,
        # context ingestion (read files -> prompt content)
        read_context_blocks,
        # CLI: the reusable runner + rendering helpers
        run_cli, NodeProgressPrinter,
        event_printer, get_console, print_results_table, print_preflight_results,
        # run configuration / settings
        RunConfig, build_run_config, get_settings, init_settings, clear_settings,
        parse_params, resolve_run_dir, default_temp_base,
        # runtime pre-flight checks
        Check, check, fatal_failures,
        # environment
        load_env,
    )

The default install is lean (pydantic, pydantic-settings, pyyaml, jsonschema,
python-dotenv) — enough to declare a pipeline and run it on the default local
backend. The opt-in extras add the heavy pieces: `agent-flow[prefect]` (the
Prefect backend) and `agent-flow[cli]` (typer + rich for run_cli / display). See
examples/ for how to build a pipeline on this library, and
docs/design/orchestrator/ for the full design.
"""

from agent_flow.backends import FlowBackend, InProcessBackend, get_backend
from agent_flow.batteries import agent_node, control_path
from agent_flow.cli import NodeProgressPrinter, event_printer, get_console, print_preflight_results, print_results_table, run_cli
from agent_flow.core import (
    AgentContentFailedError,
    AgentCrashError,
    AgentResult,
    AgentTimeoutError,
    JsonSchema,
    PydanticSchema,
    ResultSchema,
    ValidationOutcome,
    build_control_preamble,
    coerce_schema,
    load_env,
    produced,
    read_context_blocks,
    rerun_from_sidecar,
    run_agent,
)
from agent_flow.engine import (
    Node,
    NodeBlocked,
    NodeOutcome,
    RunContext,
    build_flow,
    interpret,
    plan_groups,
)
from agent_flow.flowdef import FlowDef, NodeDef, compile_flow, run_flow
from agent_flow.gates import (
    Continue,
    Directive,
    Gate,
    GateContext,
    GoTo,
    Restart,
    Stop,
    require_file,
    rerun_on_named,
    rerun_on_signal,
)
from agent_flow.preflight import Check, check, fatal_failures
from agent_flow.registry import FlowRegistry
from agent_flow.run_config import (
    RunConfig,
    build_run_config,
    clear_settings,
    get_settings,
    init_settings,
    parse_params,
    runtime_param,
    runtime_param_fields,
)
from agent_flow.run_context import (
    RunContextService,
    clear_run_context,
    get_run_context,
    init_run_context,
)
from agent_flow.runners import (
    AgentInvocation,
    AgentRunner,
    AgentRunnerInfo,
    Event,
    MockRunner,
    OpenCodeRunner,
    get_runner,
)
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
    "AgentRunnerInfo",
    "AgentInvocation",
    "Event",
    "OpenCodeRunner",
    "MockRunner",
    "get_runner",
    # run-context service (open domain params + exports)
    "RunContextService",
    "get_run_context",
    "init_run_context",
    "clear_run_context",
    # control-file protocol (injected into agent prompts)
    "build_control_preamble",
    # CLI: the reusable runner + rendering helpers
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
    "runtime_param",
    "runtime_param_fields",
    "resolve_run_dir",
    "default_temp_base",
    # result-schema seam (typed agent output)
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
    # execution backends (swappable; local default, prefect opt-in)
    "FlowBackend",
    "InProcessBackend",
    "get_backend",
    "FlowRegistry",
    "FlowDef",
    "NodeDef",
    "compile_flow",
    "run_flow",
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
    "rerun_on_named",
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
