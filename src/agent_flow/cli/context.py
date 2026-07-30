"""RunCliContext — the parameters run_cli threads into each command module.

The pipeline CLI is a reusable factory (`run_cli`), so its Typer app cannot be a
module-level singleton like a typical app — the nodes and settings are supplied
by the consumer at call time. Each command module exposes a
`register(app, ctx)` that attaches its command(s) to the shared app; this context
is the `ctx` — the consumer-supplied bits every command may need.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from agent_flow.flow_types import Node


@dataclass(frozen=True)
class RunCliContext:
    """Consumer-supplied CLI configuration, passed to each command's register()."""

    build_nodes: Callable[[], list[Node]]  # returns the pipeline's Node list
    name: str  # flow name (shown in help/summary; used as the Prefect flow name)
    llm_tag: str  # concurrency tag for node execution
    params_model: type | None  # optional pydantic-settings model for -p validation
    run_config: dict[str, Any] = field(default_factory=dict)  # the pipeline's own run-config defaults (run_config=); LOWEST explicit source
    run_instructions: str = ""  # run-wide standing brief DECLARED on the FlowDef; the -i/config instructions APPEND to it
    run_context: tuple[str, ...] = ()  # run-wide context SOURCES declared on the FlowDef (paths/globs; content read per node)
    registry: object = None  # optional FlowRegistry (named gates/exports/hooks); None -> engine default
    version: str | None = None  # the CONSUMER's app version (shown by `version`); None -> show agent-flow only
