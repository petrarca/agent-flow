---
type: ADR
title: The library↔agent agreement lives in a leaf package below core and runners
description: schema, coerce and the control protocol move out of core into protocol/, so runners never imports upward and the core↔runners cycle disappears.
tags: [agent-flow, architecture, protocol, control-file, layering]
decision: accepted
status: stable
supersedes: []
superseded_by: []
generated: { by: human:wmi, at: 2026-07-30T00:00:00Z }
---

# The library↔agent agreement lives in a leaf package below core and runners

> The shared half of a contract cannot live inside one of the two parties.

## Context

`core` supervises a run; `runners` builds the argv and assembles a result. Both
need the same two things: how an agent is told to report (the control preamble)
and what shape its answer must take (the result schema). Both lived under
`core/`, so `runners` reached back up through function-local imports and
`core ↔ runners` was a real cycle.

The recommended fix in the assessment — move `SubprocessExecutor` into `runners/`
and leave the rest — does not work. The executor needs `coerce_schema` and
`build_control_preamble`, so moving it converts two deferred imports into eager
ones and makes the cycle worse.

Inspection showed the actual shape: `control_protocol.py`, `context.py`, `env.py`
and `report_signals.py` had no `agent_flow` imports at all, and `schema.py` /
`schema_pydantic.py` were a self-contained pair. `core` was four leaves, a pair,
and one module doing all the coupling.

## Decision

We will keep the shared half of the contract in `agent_flow/protocol/` — a leaf
importing only the standard library, `jsonschema` and `pydantic` — and let both
parties depend on it:

    core → runners → protocol

The name is deliberate. It describes the *contents* (the agent protocol) rather
than the package's position in the graph. Position names — `common`, `shared`,
`kernel` — are junk-drawer magnets: "is this shared and stable?" admits anything,
while "is this part of the library↔agent agreement?" is a sharp test. The
`typing.Protocol` overlap is real but mild, and the repo already uses "protocol"
in this sense throughout.

The two halves belong together rather than merely near each other:
`build_control_preamble(agent, control_file, schema_dict)` embeds the result
schema *into* the preamble sent to the agent, so a schema is transmitted as part
of the protocol.

`coerce_schema` sits one level above the types it returns, in `protocol/coerce.py`.
A factory must know every implementation while each implementation needs only the
shared outcome type; keeping it beside the types forced a function-local import
and made a second cycle.

## Consequences

`runners` imports nothing from `core`. The deferred imports that existed only to
dodge the cycle are now ordinary module-level ones, so the dependency is visible
in the import graph rather than hidden in function bodies.

`SubprocessExecutor` can then sit in `runners/` beside its three siblings, making
`get_executor` a flat dispatch — which is what `ServeExecutor` needs.

Accepted trade-offs. `core` no longer re-exports these names, so
`from agent_flow.core import coerce_schema` is gone; the public `agent_flow`
exports are unchanged and consumers are unaffected. One more top-level package
in a tree that already has several, justified because the layering test now
requires every package to be placed deliberately.

Enforced by the `protocol is a leaf` contract in ADR-0001.
