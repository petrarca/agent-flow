---
type: ADR
title: The FlowRegistry is run-scoped and reaches a node on the RunContext
description: Removing registry= from agent_node closes a path where omitting it silently ran a real agent under --mock-agents.
tags: [agent-flow, registry, run-context, node-builder, mock-agents]
decision: accepted
status: stable
supersedes: []
superseded_by: []
generated: { by: human:wmi, at: 2026-07-30T00:00:00Z }
---

# The FlowRegistry is run-scoped and reaches a node on the RunContext

> Two paths to one value, and they could disagree silently.

## Context

`agent_node` took `registry=` and used it to resolve the work-order renderer, the
prompt renderer and mock agents. `build_flow` also takes a registry — and creates
a default when none is given — and `interpret` already received one and already
constructed the `RunContext`. The value was threaded to within one line of where
the node needed it, and restated per node anyway.

Omitting it did not fail. It changed execution: with `--mock-agents` on,
executor selection found no registry, skipped the mock and ran the real agent.
Measured directly, same flow, same `build_flow(registry=…)` and `mock_agents=True`,
differing only in whether the node builder was given the argument:

    registry passed    -> mock invoked = True
    registry OMITTED   -> mock invoked = False

The second run completed and reported success, because `opencode` was on PATH —
so a flow in mock mode spent real tokens. The declarative path was safe
(`flowdef/compile.py` always passed it); only hand-written flows were exposed,
which is the path with no compiler to catch the omission.

No call site in the library, its tests or its examples has ever wanted two
registries in one flow.

## Decision

We will carry the registry on `RunContext` and remove `registry=` from
`agent_node`. `interpret` puts the registry it already holds onto the context it
already builds; the node closure reads `ctx.registry`.

## Consequences

There is no longer a way to build the node "wrong" — the divergence is removed by
construction rather than documented. `agent_node` drops from 21 parameters to 20,
which is a side effect and not the reason.

A Tier-2 caller driving a node directly supplies the registry the same way, on
the context it constructs.

Accepted trade-off: a breaking change to a public function. It is pre-1.0, the
one known consumer does not call `agent_node`, and the alternative — keeping the
parameter as an override — would have preserved two paths that can disagree, just
with a documented precedence.
