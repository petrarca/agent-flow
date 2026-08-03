"""`AgentInvocation` — one neutral request to run an agent, and its composition.

The single currency between the node builder and the executors: WHAT to run, WITH
what prompt and inputs, WHERE (run_dir / agent_dir), and under what liveness
budget. Every executor takes this same object, so the engine never learns which
one it got.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from agent_flow.const import DEFAULT_IDLE_TIMEOUT_S as DEFAULT_IDLE_TIMEOUT_S
from agent_flow.protocol import RerunSpec
from agent_flow.runners.events import Event
from agent_flow.runners.prompt import PromptParts

if TYPE_CHECKING:
    from upath import UPath


class AgentRunnerInfo(BaseModel):
    """Diagnostic self-description of a runner's runtime (best-effort).

    Returned by the optional `AgentRunner.info()` for a doctor/summary view — it
    reports what the RUNTIME says about itself, never library-invented values. A
    field is left None/[] when the runtime cannot be introspected — either
    transiently (binary missing, command failed) or STRUCTURALLY, when the
    runtime exposes no way to resolve it from a CLI. E.g. opencode reports the
    resolved model/tools via `opencode debug config`, but Claude Code has no
    equivalent CLI subcommand (resolution is SDK-only), so its info() may report
    version/availability only and leave resolved_model=None, tools=[]. A None
    field therefore means "not introspectable here", not "misconfigured".
    info() must never raise.
    """

    name: str = Field(description="Runner id (e.g. 'opencode', 'mock', 'claude').")
    available: bool = Field(default=False, description="Is the runtime usable (binary found / importable)?")
    version: str | None = Field(default=None, description="Runtime version, if it reports one.")
    resolved_model: str | None = Field(default=None, description="The model the RUNTIME would actually use (its own default); None if unknown.")
    tools: list[str] = Field(default_factory=list, description="Tools / MCP servers the runtime exposes, if it can report them.")
    detail: str = Field(default="", description="Freeform notes (path, config source, error hints).")


@dataclass(frozen=True)
class AgentInvocation:
    """The complete, runtime-NEUTRAL request to run ONE agent — the seam's input.

    This is the single input to an `AgentExecutor.run(inv) -> AgentResult`. It
    carries EVERYTHING an executor needs, and NOTHING that is specific to one
    execution mechanism: a subprocess executor and an in-process executor receive
    the exact same invocation. (The subprocess control sidecar is deliberately
    NOT here — it is a SubprocessExecutor-private detail derived from run_dir +
    agent, along with the control preamble it injects.)

    Prompt layering. `prompt` is the fully-composed PER-NODE prompt (per-node
    context + instructions + the one-time instruction + the work order — already
    joined by agent_node). `run_context` / `run_instructions` are the
    RUN-WIDE blocks, kept separate so a single composer (`compose_prompt`) lays
    the final order in ONE place: [run_context][run_instructions][prompt].
    A subprocess executor additionally prepends the control preamble (its own
    mechanism); an in-process executor uses the composed prompt as-is.

    Agent identity. `agent` is the logical name/ref; `instructions` is the
    resolved standing context for runtimes WITHOUT named agents (opencode ignores
    it — its identity lives in its .md; Claude Code injects it as a system
    prompt). Executors materialise identity their own way from these fields.

    Text AND data. A subprocess agent can only be handed TEXT, so `prompt` is the
    contract that matters for it. An IN-PROCESS agent is Python calling Python and
    wants the values themselves, so the same request is also carried structured:
    `inputs` (this node's resolved work order) and `params` (the run's domain
    params). Both are already templated — the exact values that were rendered into
    the prompt — so an impl never has to parse them back out of the text it was
    given. The subprocess path simply ignores them.
    """

    agent: str  # logical agent name / ref
    prompt: str  # fully-composed per-node prompt (context+instructions+one-time+work order)
    run_dir: Path | UPath  # the run's directory (artifact/sidecar root; base for relative paths). UPath for an in-memory mock run.
    node: str = ""  # the NODE this invocation runs (neutral identity; unique per run).
    # Used e.g. by SubprocessExecutor to key its per-node control sidecar
    # ("<node>.control.json"). Falls back to `agent` when empty.
    result_schema: object = None  # ResultSchema | JSON-schema dict | pydantic model; typed output contract
    model: str | None = None
    agent_dir: str = ""  # absolute dir where agent DEFINITIONS live (opencode: --dir); "" = runtime default
    instructions: str = ""  # resolved standing instructions (for runners without named agents)
    run_instructions: str = ""  # run-wide brief injected into every agent
    run_context: str = ""  # run-wide context CONTENT (already read from files)
    idle_timeout_s: int = DEFAULT_IDLE_TIMEOUT_S  # liveness budget (subprocess) / cap hint (in-process)
    on_event: Callable[[Event], None] | None = None  # live progress callback (both kinds may emit)
    # The STRUCTURED twin of `prompt` (see "Text AND data" above). Both are
    # snapshots owned by this invocation — an impl may read them freely.
    inputs: dict[str, str] = field(default_factory=dict)  # this node's resolved work order ({KEY: value}, templated)
    input_obj: object = None  # `inputs` validated against the node's input_schema (a pydantic instance), else None
    # The channels this prompt was rendered FROM, when a renderer produced it
    # (Tier 3 / agent_node). None means `prompt` is a caller's raw text and the
    # run-wide blocks still have to be prepended — see compose_prompt.
    parts: PromptParts | None = None
    params: dict[str, Any] = field(default_factory=dict)  # the run's domain params (incl. upstream `exports`)
    # The node's re-run GRANT (protocol.RerunSpec), when it declared
    # `rerun_targets`. Carried here because the preamble that TELLS the agent
    # about the lever is built at the executor seam, which has only this
    # invocation — the DAG that knows the legal targets lives two tiers up, so
    # the grant travels down as data. None = not granted (the common case).
    rerun: RerunSpec | None = None


def compose_prompt(inv: AgentInvocation) -> str:
    """The COMPLETE runtime-neutral prompt for an invocation (minus the
    subprocess control preamble, which SubprocessExecutor prepends).

    Two kinds of invocation reach here, and conflating them double-injects the
    run-wide blocks:

    - **Rendered (Tier 3 / `agent_node`)** — `parts` is set, so `prompt` is
      already the full body a renderer produced from every channel. Return it
      unchanged; prepending here would repeat the run-wide context and brief.
    - **Raw (Tier 1/2 / `run_agent`)** — the caller passed their own `prompt`
      text plus `run_context` / `run_instructions` as separate arguments, so the
      run-wide blocks are prepended here in the documented order.
    """
    if inv.parts is not None:
        return inv.prompt
    blocks: list[str] = []
    if inv.run_context and inv.run_context.strip():
        blocks.append(f"## Run-wide context\n\n{inv.run_context.strip()}")
    if inv.run_instructions and inv.run_instructions.strip():
        blocks.append(f"## Run-wide instructions\n\n{inv.run_instructions.strip()}")
    blocks.append(inv.prompt)
    return "\n\n".join(blocks)
