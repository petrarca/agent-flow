"""Library-wide constants — the purest leaf in the package.

This module imports NOTHING from `agent_flow` (only the stdlib), so every tier
may depend on it without risking a cycle or an upward import. It exists because a
few values are needed by consumers on both sides of the tier boundary:

  - `DEFAULT_IDLE_TIMEOUT_S` is a field default on `AgentInvocation` (Tier 1,
    `runners.base`) AND the run-wide fallback the engine (Tier 3) resolves.
  - `DEFAULT_DURATIONS` maps the portable duration NAMES a `NodeDef` declares to
    seconds; it is read by the engine's build-time validation (Tier 3) and by the
    node builder (Tier 2), and re-exported on the public API.

Putting them here keeps the number `120` written ONCE — `DEFAULT_DURATIONS`
references `DEFAULT_IDLE_TIMEOUT_S` directly, so "normal" and the run-wide default
cannot drift. Behaviour (templating, run-dir resolution, the optional-dep guard)
stays in `utils.py`; this module is data only.
"""

from __future__ import annotations

# Liveness / timeout budget default (seconds). The subprocess executor treats it
# as an idle deadline (kill only after this long with no event/sidecar); an
# in-process executor may use it as a wall-clock cap hint. Generous because real
# LLM agents can pause 60-90s between tool calls; tune per run via the CLI
# --idle-timeout / AGENT_FLOW_IDLE_TIMEOUT_S, or per node via a `duration`.
DEFAULT_IDLE_TIMEOUT_S = 120

# Duration VOCABULARY -> seconds. A flow declares portable INTENT
# (`NodeDef.duration="long"` — "this node writes long reports"); the run config
# supplies the concrete seconds (`durations: {long: 900}`). That split keeps a
# serialized flow meaningful on a machine other than the one it was written on.
# Shipped so a flow runs with zero configuration; a run config's `durations`
# merges OVER this map (see utils.duration_table), so a consumer can retune a
# shipped name and add its own. An unknown name is a hard error, never a silent
# fallback. "normal" IS the run-wide default, reached by name instead of number.
DEFAULT_DURATIONS: dict[str, int] = {"short": 60, "normal": DEFAULT_IDLE_TIMEOUT_S, "long": 600}

# Retries after a TRANSIENT agent failure — the agent hung (stale-killed on the
# liveness deadline above) or its process crashed. Applied PER NODE, so a retried
# node's parallel siblings are unaffected. TWO retries by default, because the
# failure this guards against is a stall, and a stall costs the FULL idle timeout
# (10 minutes for a `long` node) before it is even detected: by the time one
# retry is spent, a long node has burned ~20 minutes and the run is far enough in
# that losing the node — and everything downstream that reads its output — is the
# expensive outcome, not the extra attempt. Three consecutive stalls is evidence
# of a real problem rather than a blip, at which point the node's `criticality`
# decides whether the run continues (degrade) or stops (blocking). Tune per run
# via AGENT_FLOW_MAX_RETRIES / the run config, or per node via
# `nodes.<n>.max_retries`.
# A failure the agent DIAGNOSED itself is never retried, whatever this says.
DEFAULT_MAX_RETRIES = 2
