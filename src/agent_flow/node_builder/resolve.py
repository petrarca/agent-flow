"""Per-node SETTING resolution — the precedence chains.

Four settings can be declared in two places: on the flow (`agent_node(...)`) and
on the run (`RunConfig.nodes.<name>`). This module owns the resulting precedence
in ONE place, so the rules are readable as a unit rather than interleaved with
prompt assembly and executor wiring inside the node closure.
"""

from __future__ import annotations

from dataclasses import dataclass

from agent_flow.flow_types import RunContext
from agent_flow.runners import probe_agent_dir
from agent_flow.runners.invocation import DEFAULT_IDLE_TIMEOUT_S
from agent_flow.utils import resolve_duration, resolve_template


@dataclass(frozen=True)
class NodeSettings:
    """The four run-time settings a node resolves before it can be invoked."""

    runtime: str
    agent_dir: str
    model: str
    duration: str
    idle_timeout_s: int


def resolve_node_settings(
    *,
    name: str,
    ctx: RunContext,
    ov: dict,
    tmpl: dict,
    agent_dir: str | None,
    model: str | None,
    duration: str | None,
) -> NodeSettings:
    """Resolve runtime, agent_dir, model, duration and the liveness budget.

    `ov` is this node's per-node run-config entry (may be empty); it is the most
    specific source in every chain below.
    """
    runtime = ctx.params.get("runtime", "opencode")
    # Per-setting precedence (most specific first): the run config's per-node
    # entry (this run) > the agent_node() arg (the flow's standing declaration)
    # > the run-wide value > the RUNNER PROBE > (empty -> preflight error).
    # `ov` is that per-node entry. agent_dir is templated; the probe is the
    # comfort fallback so the common case needs no explicit agent_dir at all.
    explicit_agent_dir = ov.get("agent_dir") or agent_dir or ctx.agent_dir or ""
    eff_agent_dir = resolve_template(explicit_agent_dir, tmpl) if explicit_agent_dir else (probe_agent_dir(runtime) or "")
    # model: empty ("") means "no model" — the runner omits --model and the
    # runtime resolves it (never a hardcoded one).
    eff_model = ov.get("model") or model or ctx.params.get("model") or ""
    # Liveness budget resolution, most specific first:
    #   1. the per-node run-config idle_timeout_s (a raw second-count override),
    #   2. the per-node duration NAME (run config's, then the flow-declared),
    #   3. the run-wide idle timeout (rides `params`, read per node at run time),
    #   4. the library default.
    # build_flow already rejected an unknown duration name; the resolve here is
    # also the sole guard when a Tier-2 flow calls interpret() without build_flow.
    run_wide_idle = int(ctx.params.get("idle_timeout_s") or DEFAULT_IDLE_TIMEOUT_S)
    eff_duration = ov.get("duration") or duration
    if ov.get("idle_timeout_s") is not None:
        eff_idle = int(ov["idle_timeout_s"])
    elif eff_duration:
        eff_idle = resolve_duration(name, eff_duration, ctx.durations)
    else:
        eff_idle = run_wide_idle
    return NodeSettings(
        runtime=runtime,
        agent_dir=eff_agent_dir or "",
        model=eff_model,
        duration=eff_duration or "",
        idle_timeout_s=eff_idle,
    )
