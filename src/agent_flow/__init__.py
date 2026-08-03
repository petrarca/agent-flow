"""agent-flow — deterministic orchestration of coding-agent pipelines.

A small library that replaces LLM "orchestrator agents" with a deterministic
engine: declare a pipeline as typed data and the library runs it, supervising
each agent as a subprocess (liveness, kill, sidecar status), with parallelism,
bounded re-runs, criticality, and telemetry. The execution backend (Prefect) and
the agent runtime (opencode, Claude Code, …) are both pluggable.

Public API (the authoritative list is `__all__` below):

    from agent_flow import (
        # Tier 1: one supervised agent (the primitive; arun_agent = async native)
        run_agent, arun_agent, AgentResult,
        AgentTimeoutError, AgentContentFailedError, AgentCrashError,
        # the retry taxonomy: raise these from your own `run` to opt in/out
        AgentError, TransientAgentError, PermanentAgentError,
        # runners (subprocess wire adapters) + the execution seam
        AgentRunner, AgentInvocation, Event,
        AgentExecutor, InProcessExecutor, AgentImpl, get_executor, compose_prompt,
        MockExecutor, MockAgentContext, MockAgent,  # --mock-agents mode
        OpenCodeRunner, get_runner, probe_agent_dir,
        # Tier 3: declare Nodes -> build_flow (the DAG engine)
        Node, NodeOutcome, RunContext, build_flow, NodeBlocked,
        plan_groups, interpret,
        # node builder: one-call node for the common "run one agent" case
        agent_node, control_path,
        # flow-control gates — the consumer's optional hook
        Gate, GateContext, Directive,
        Continue, Restart, GoTo, Stop,
        require_file, stop_if,
        # file-based signals (gate building blocks)
        produced, read_field,
        # agent-requested re-run (declared per node via rerun_targets)
        RerunSpec, RerunRequest, parse_rerun,
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
        RunConfig, NodeRunConfig, build_run_config,
        parse_params, resolve_run_dir, default_temp_base,
        # duration vocabulary (portable node intent -> seconds)
        DEFAULT_DURATIONS, duration_table,
        # runtime pre-flight checks
        Check, check, fatal_failures,
        # environment
        load_env,
    )

The default install is lean (anyio, loguru, pydantic, pydantic-settings, pyyaml,
jsonschema, python-dotenv, universal-pathlib) — enough to declare a pipeline and
run it on the default local backend. The opt-in extras add the heavy pieces: `petrarca-agent-flow[prefect]`
(the Prefect backend) and `petrarca-agent-flow[cli]` (typer + rich for run_cli /
display). See
examples/ for how to build a pipeline on this library, and
docs/design/ for the full design.
"""

import importlib
from typing import TYPE_CHECKING

from loguru import logger as _loguru_logger

from agent_flow._version import __version__

# Eager, and only this: the loguru side effect below needs the value at import
# time. logging_setup is an 85-line leaf with no agent_flow imports, so this
# does not pull the library in — everything else resolves through __getattr__.
from agent_flow.logging_setup import LIBRARY_LOGGER

if TYPE_CHECKING:  # the static view: type checkers and IDEs see the real names
    from agent_flow.backends import FlowBackend, InProcessBackend, get_backend
    from agent_flow.cli import NodeProgressPrinter, event_printer, get_console, print_preflight_results, print_results_table, run_cli
    from agent_flow.core import AgentResult, arun_agent, load_env, read_context_blocks, run_agent
    from agent_flow.engine import build_flow, interpret, plan_groups
    from agent_flow.errors import AgentError, PermanentAgentError, TransientAgentError
    from agent_flow.flow_types import Node, NodeBlocked, NodeOutcome, RunContext
    from agent_flow.flowdef import FlowDef, NodeDef, arun_flow, compile_flow, run_flow
    from agent_flow.gates import (
        Continue,
        Directive,
        Gate,
        GateContext,
        GoTo,
        Restart,
        Stop,
        produced,
        read_field,
        require_file,
        stop_if,
    )
    from agent_flow.logging_setup import LIBRARY_LOGGER, setup_logging
    from agent_flow.node_builder import (
        DEFAULT_WORK_ORDER_RENDERER,
        agent_node,
        build_work_order,
        control_path,
        render_work_order_lines,
        render_work_order_xml,
    )
    from agent_flow.preflight import Check, check, fatal_failures
    from agent_flow.protocol import (
        JsonSchema,
        PydanticSchema,
        RerunRequest,
        RerunSpec,
        ResultSchema,
        ValidationOutcome,
        build_control_preamble,
        coerce_schema,
        parse_rerun,
    )
    from agent_flow.registry import FlowRegistry
    from agent_flow.run_config import NodeRunConfig, RunConfig, build_run_config, parse_params, runtime_param, runtime_param_fields
    from agent_flow.run_context import RunContextService, clear_run_context, get_run_context, init_run_context
    from agent_flow.runners import (
        AgentExecutor,
        AgentImpl,
        AgentInvocation,
        AgentRunner,
        AgentRunnerInfo,
        Event,
        InProcessExecutor,
        MockAgent,
        MockAgentContext,
        MockExecutor,
        OpenCodeRunner,
        PromptParts,
        compose_prompt,
        get_executor,
        get_runner,
        probe_agent_dir,
        render_prompt,
    )
    from agent_flow.runners.executor import AgentContentFailedError, AgentCrashError, AgentTimeoutError
    from agent_flow.utils import DEFAULT_DURATIONS, default_temp_base, duration_table, resolve_run_dir

# name -> the module that owns it. The facade resolves an attribute on first
# access (PEP 562) instead of importing all eighteen modules at import time.
_EXPORTS: dict[str, str] = {
    "AgentContentFailedError": "agent_flow.runners.executor",
    "AgentCrashError": "agent_flow.runners.executor",
    "AgentError": "agent_flow.errors",
    "AgentExecutor": "agent_flow.runners",
    "AgentImpl": "agent_flow.runners",
    "AgentInvocation": "agent_flow.runners",
    "AgentResult": "agent_flow.core",
    "AgentRunner": "agent_flow.runners",
    "AgentRunnerInfo": "agent_flow.runners",
    "AgentTimeoutError": "agent_flow.runners.executor",
    "Check": "agent_flow.preflight",
    "Continue": "agent_flow.gates",
    "DEFAULT_DURATIONS": "agent_flow.utils",
    "DEFAULT_WORK_ORDER_RENDERER": "agent_flow.node_builder",
    "Directive": "agent_flow.gates",
    "Event": "agent_flow.runners",
    "FlowBackend": "agent_flow.backends",
    "FlowDef": "agent_flow.flowdef",
    "FlowRegistry": "agent_flow.registry",
    "Gate": "agent_flow.gates",
    "GateContext": "agent_flow.gates",
    "GoTo": "agent_flow.gates",
    "InProcessBackend": "agent_flow.backends",
    "InProcessExecutor": "agent_flow.runners",
    "JsonSchema": "agent_flow.protocol",
    "LIBRARY_LOGGER": "agent_flow.logging_setup",
    "MockAgent": "agent_flow.runners",
    "MockAgentContext": "agent_flow.runners",
    "MockExecutor": "agent_flow.runners",
    "Node": "agent_flow.flow_types",
    "NodeBlocked": "agent_flow.flow_types",
    "NodeDef": "agent_flow.flowdef",
    "NodeOutcome": "agent_flow.flow_types",
    "NodeProgressPrinter": "agent_flow.cli",
    "NodeRunConfig": "agent_flow.run_config",
    "OpenCodeRunner": "agent_flow.runners",
    "PermanentAgentError": "agent_flow.errors",
    "PromptParts": "agent_flow.runners",
    "PydanticSchema": "agent_flow.protocol",
    "RerunRequest": "agent_flow.protocol",
    "RerunSpec": "agent_flow.protocol",
    "Restart": "agent_flow.gates",
    "ResultSchema": "agent_flow.protocol",
    "RunConfig": "agent_flow.run_config",
    "RunContext": "agent_flow.flow_types",
    "RunContextService": "agent_flow.run_context",
    "Stop": "agent_flow.gates",
    "TransientAgentError": "agent_flow.errors",
    "ValidationOutcome": "agent_flow.protocol",
    "agent_node": "agent_flow.node_builder",
    "arun_agent": "agent_flow.core",
    "arun_flow": "agent_flow.flowdef",
    "build_control_preamble": "agent_flow.protocol",
    "build_flow": "agent_flow.engine",
    "build_run_config": "agent_flow.run_config",
    "build_work_order": "agent_flow.node_builder",
    "check": "agent_flow.preflight",
    "clear_run_context": "agent_flow.run_context",
    "coerce_schema": "agent_flow.protocol",
    "compile_flow": "agent_flow.flowdef",
    "compose_prompt": "agent_flow.runners",
    "control_path": "agent_flow.node_builder",
    "default_temp_base": "agent_flow.utils",
    "duration_table": "agent_flow.utils",
    "event_printer": "agent_flow.cli",
    "fatal_failures": "agent_flow.preflight",
    "get_backend": "agent_flow.backends",
    "get_console": "agent_flow.cli",
    "get_executor": "agent_flow.runners",
    "get_run_context": "agent_flow.run_context",
    "get_runner": "agent_flow.runners",
    "init_run_context": "agent_flow.run_context",
    "interpret": "agent_flow.engine",
    "load_env": "agent_flow.core",
    "parse_params": "agent_flow.run_config",
    "parse_rerun": "agent_flow.protocol",
    "plan_groups": "agent_flow.engine",
    "print_preflight_results": "agent_flow.cli",
    "print_results_table": "agent_flow.cli",
    "probe_agent_dir": "agent_flow.runners",
    "produced": "agent_flow.gates",
    "read_context_blocks": "agent_flow.core",
    "read_field": "agent_flow.gates",
    "render_prompt": "agent_flow.runners",
    "render_work_order_lines": "agent_flow.node_builder",
    "render_work_order_xml": "agent_flow.node_builder",
    "require_file": "agent_flow.gates",
    "resolve_run_dir": "agent_flow.utils",
    "run_agent": "agent_flow.core",
    "run_cli": "agent_flow.cli",
    "run_flow": "agent_flow.flowdef",
    "runtime_param": "agent_flow.run_config",
    "runtime_param_fields": "agent_flow.run_config",
    "setup_logging": "agent_flow.logging_setup",
    "stop_if": "agent_flow.gates",
}


def __getattr__(name: str) -> object:
    """Import the module that owns `name` on first access, then cache it.

    `import agent_flow` is a Tier-1 consumer's entry point as much as a Tier-3
    one's, and eager re-exports made it construct the whole library — the CLI,
    both backends and every runner — to reach `run_agent`. Resolving lazily makes
    the three-tier design true at import time as well as at API level, and keeps
    the cost of a new runner module off every consumer.
    """
    module = _EXPORTS.get(name)
    if module is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(importlib.import_module(module), name)
    globals()[name] = value  # cache: subsequent lookups skip __getattr__ entirely
    return value


def __dir__() -> list[str]:
    return sorted(__all__)


# A library must not write to stderr unless the application asks it to. loguru
# ships an ENABLED default stderr sink, so importing agent-flow would otherwise
# spam a programmatic consumer with our INFO/DEBUG records. Follow loguru's
# documented library pattern: disable our own records at import; `setup_logging`
# (which run_cli calls, and any consumer may call) re-enables them.
_loguru_logger.disable(LIBRARY_LOGGER)

__all__ = [
    "__version__",
    # agent execution
    "run_agent",
    "arun_agent",
    "AgentResult",
    "AgentTimeoutError",
    "AgentContentFailedError",
    "AgentCrashError",
    # retry taxonomy (a custom `run` raises these to opt into / out of retrying)
    "AgentError",
    "TransientAgentError",
    "PermanentAgentError",
    # runners + execution seam
    "AgentRunner",
    "AgentRunnerInfo",
    "AgentInvocation",
    "AgentExecutor",
    "InProcessExecutor",
    "AgentImpl",
    "MockExecutor",
    "MockAgentContext",
    "MockAgent",
    "Event",
    "OpenCodeRunner",
    "get_runner",
    "get_executor",
    "probe_agent_dir",
    "compose_prompt",
    # run-context service (open domain params + exports)
    "RunContextService",
    "get_run_context",
    "init_run_context",
    "clear_run_context",
    # control-file protocol (injected into agent prompts)
    "build_control_preamble",
    # agent-requested re-run (granted per node via rerun_targets)
    "RerunSpec",
    "RerunRequest",
    "parse_rerun",
    # CLI: the reusable runner + rendering helpers
    "event_printer",
    "get_console",
    "print_results_table",
    "print_preflight_results",
    "NodeProgressPrinter",
    "run_cli",
    "RunConfig",
    "NodeRunConfig",
    "build_run_config",
    "parse_params",
    "runtime_param",
    "runtime_param_fields",
    "resolve_run_dir",
    "DEFAULT_DURATIONS",
    "duration_table",
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
    "arun_flow",
    # node builder: one-call node for the common "run one agent" case
    "agent_node",
    "build_work_order",
    # prompt rendering (override via FlowRegistry.prompt / .work_order)
    "PromptParts",
    "render_prompt",
    "render_work_order_xml",
    "render_work_order_lines",
    "DEFAULT_WORK_ORDER_RENDERER",
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
    "stop_if",
    # signals
    "produced",
    "read_field",
    # pre-flight checks
    "Check",
    "check",
    "fatal_failures",
    # env
    "load_env",
    # logging (loguru + stdlib intercept)
    "setup_logging",
]
