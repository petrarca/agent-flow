---
type: Concept
title: The agent-requested re-run — a granted, declared capability
description: How an agent asks for an earlier step to run again via `rerun_required`, and how a node grants that with `rerun_targets`.
tags: [agent-flow, rerun, flow-control, protocol, control-file, capability]
timestamp: 2026-08-03T00:00:00Z
---

# The agent-requested re-run

An agent's only lever on the flow is to ask that an **earlier step run again**.
It writes `rerun_required` in its [control file](control-file.md); the engine
honors it — but only where the node **granted** the lever.

```python
NodeDef(
    name="consistency-check",
    agent="consistency-check",
    depends_on=["ai-leverage"],
    rerun_targets=["tech-stack", "analysis", "architecture"],   # the GRANT
)
```

That single declaration does all three jobs:

1. **Grants** the capability — without it the field is ignored.
2. **Names the legal targets** in the agent's preamble, so an agent never
   hardcodes step names.
3. **Authorizes** the jump — the engine honors a request for one of these.

## No gate is involved

A [gate](gates.md) exists for decisions a declaration cannot express — "stat this
file", "read that field". Here there is nothing to decide: the consumer already
expressed intent by declaring `rerun_targets`. A gate would be pure boilerplate
(*"if the agent asked, jump"*), so the engine applies the rule directly, the way
it applies `depends_on` or `max_cycles`.

This does **not** contradict "the engine never auto-fails, a gate does". That
rule exists so the engine never *infers* flow control from artifacts or schemas
nobody asked it to police. Honoring an explicit declaration is the opposite of
inference.

**The gate remains the override.** It still runs first; if it returns anything
other than `Continue`, that decision stands and the request is not acted on. The
declaration is the default, the gate the escape hatch.

## What the agent writes

```
rerun_required:  true | "<target>" | { target?, instruction? }
```

| Shape | Meaning |
|---|---|
| `true` | re-run the sole granted target (only valid when exactly one was granted) |
| `"domain"` | re-run that target |
| `{ "target": "domain", "instruction": "recompute the coupling figure" }` | re-run it, with one-time guidance |

`target` is **optional when one target was granted** and **required when several
were**. With a single grant there is no choice to make, so demanding the agent
echo a name it cannot get wrong is ceremony that only adds a failure mode. The
rule is applied by the parser, which fills in the sole target when one was
granted and refuses an unresolvable request when several were.

The `instruction` is handed to the target **verbatim**, as the last block of its
next prompt — the freshest guidance, right before its work order. It is
ephemeral: one attempt only.

### Deliberately singular

One request names one target. The machinery is singular all the way down: a
jump-back resolves to one `GoTo`, which carries one node. A list would promise
something the engine cannot honor. *"Re-run several nodes"* is expressed by
naming their parallel **group**.

## Targets: nodes or groups

A target is a node name **or** a parallel-group name.

- **Node** — re-runs exactly that node. Its parallel siblings keep their results.
- **Group** — expands to its members: the whole wave re-runs, and the
  instruction is **broadcast to every member**. Choosing the group (over one of
  its members) is itself the claim that the reason applies to the wave.

Each member is then bounded by its **own** `max_cycles`: an exhausted member is
skipped while its eligible siblings still re-run.

Because a group name is opaque on its own, the preamble spells out what it
covers:

```
  - tech-stack
  - analysis  (runs: security, domain, coupling)
  - architecture
```

## Backward only

A re-run resumes at something that **already ran**, so every target must lie
before the declaring node. This is validated at **build time** — an unknown or
forward target fails `build_flow`, rather than being silently dropped at the jump
(which is what a stale name in an agent's `.md` used to do: the walker ignores an
unknown target, so the re-run simply never happened).

After the jump the flow **re-flows forward** through everything downstream. So an
agent names only the ROOT CAUSE; naming the cascade by hand is neither needed nor
correct.

## The grant is an allowlist, and it is enforced

Declaring `rerun_targets` does not merely *advertise* the legal targets — it
**restricts** them. A request naming anything else is refused and logged, even
when it names a perfectly valid backward node the DAG could otherwise honor:

```
node consistency-check: agent requested re-run of 'readiness', which it was not
granted (allowed: ['tech-stack', 'analysis', ...]) — ignoring
```

The distinction matters. The walker's own checks — known / backward / not
exhausted — are about what the **graph** can do. This one is about what this
**agent was permitted to ask for**, and it lives in the engine
(`_with_rerun_request`) so there is exactly one place that decides. Without it an
LLM naming something plausible-but-ungranted would steer the flow, which is
precisely what granting is supposed to prevent.

Refusal is never fatal: the node settles normally and the run continues.

## Where it lives

The sidecar has three zones with three readers, and the re-run field is its own:

| field(s) | reader | what it is |
|---|---|---|
| `status` / `agent` / `reason` | the **engine** | the success verdict |
| `rerun_required` | the **engine, only where granted** | flow control |
| `result` | the **application** | domain data (never read by the engine) |

## How it flows

```
build_flow      validate rerun_targets (known + backward), expand groups   → RerunSpec
RunContext      the resolved grant reaches the node's run
AgentInvocation carries it to the executor
preamble        the re-run block naming the legal targets (granted nodes only)
agent           writes rerun_required
interpret       gate first; on Continue, parse the request → GoTo
walker          expand group → members, bound per member, deliver instruction
```

The grant is resolved **once**, in `build_flow`, because only there is the DAG
known. It then travels down as data — the preamble is built at the executor seam
(Tier 1), which must not reach up to the engine.

## Code

`src/agent_flow/protocol/rerun.py` (`RerunSpec`, `parse_rerun`),
the block in `protocol/control.py`, `Node.rerun_targets` in `flow_types.py`,
`_resolve_rerun_grants` in `engine/builder.py`, `_with_rerun_request` in
`engine/interpreter.py`, and the group expansion in `engine/walker.py`.
