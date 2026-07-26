"""In-process agents — a 2-node flow with NO subprocess, NO sidecar.

Both "agents" are plain Python functions that simulate a PydanticAI-style agent:
they take the neutral `AgentInvocation` (the composed prompt + params) and return
a typed pydantic model. agent-flow's `InProcessExecutor` runs them as direct
calls and maps the typed return onto the same `AgentResult` a subprocess agent
would produce — so gates read `ctx.obj` identically, and nothing spawns a process
or writes a control file.

The pipeline:

    classify  ->  respond

  - classify: reads the ticket text from the work order, returns a
    Classification{category, urgency}.
  - respond:  reads the classification (published downstream via `exports`),
    returns a DraftReply{channel, message}.

Two ways to attach an in-process impl are shown:
  - by NAME on the FlowDef  (NodeDef.impl_ref + registry.agent_impl) — declarative,
    the definition stays serializable;
  - (see the comment on `classify`) the imperative agent_node(impl=fn) form.

Because every node is in-process, this example runs the flow PROGRAMMATICALLY via
`run_flow` — there is no runtime to select, no agent-dir, and the opencode
pre-flight (which the CLI `run_cli` applies) does not apply. `flow nodes`-style
introspection is still available via `compile_flow` if you want it.

Run:
    python -m examples.inprocess "the app crashes on login, urgent"
    python -m examples.inprocess            # uses the default ticket
"""

from __future__ import annotations

from pydantic import BaseModel

from agent_flow import AgentInvocation, FlowDef, FlowRegistry, NodeDef


# --- Typed outputs (what each "agent" returns) ------------------------------
class Classification(BaseModel):
    category: str
    urgency: str  # "low" | "normal" | "high"


class DraftReply(BaseModel):
    channel: str
    message: str


# --- The registry: two in-process agent impls + their result schemas --------
# A NodeDef references these BY NAME (impl_ref / result_schema), so the FlowDef
# stays pure serializable data while the code lives here.
REGISTRY = FlowRegistry()

REGISTRY.schema("Classification")(Classification)
REGISTRY.schema("DraftReply")(DraftReply)


@REGISTRY.agent_impl("classify")
def classify(inv: AgentInvocation) -> Classification:
    """A mock PydanticAI-style agent: ticket text in, typed classification out.

    Receives the FULL neutral invocation (same as a subprocess agent would). A
    real PydanticAI agent would `await agent.run(inv.prompt)`; here we do trivial
    keyword heuristics so the example is deterministic and dependency-free.

    (Imperatively this same function could be attached with
    `agent_node("classify", "classify", impl=classify)`; here it is referenced
    by name from the FlowDef via `impl_ref="classify"`.)
    """
    text = inv.prompt.lower()
    urgent = any(w in text for w in ("urgent", "crash", "down", "cannot", "can't", "asap"))
    category = "bug" if any(w in text for w in ("crash", "error", "bug", "broken")) else "general"
    return Classification(category=category, urgency="high" if urgent else "normal")


@REGISTRY.agent_impl("respond")
def respond(inv: AgentInvocation) -> DraftReply:
    """A second mock agent: drafts a reply from the upstream classification.

    The classify node publishes `category`/`urgency` downstream via `exports`
    (see the FlowDef). The engine templates them into THIS node's work-order
    inputs ({category}/{urgency}), so they arrive inside the composed `inv.prompt`
    — which is all an in-process impl receives. We parse them back out below; a
    real agent would just reason over `inv.prompt` as its input text.
    """
    category = inv_params(inv).get("category", "general")
    urgency = inv_params(inv).get("urgency", "normal")
    channel = "phone" if urgency == "high" else "email"
    message = f"Thanks for reporting this {category} issue. " + (
        "We are treating it as urgent and will call you shortly." if urgency == "high" else "We will follow up by email within one business day."
    )
    return DraftReply(channel=channel, message=message)


def inv_params(inv: AgentInvocation) -> dict:
    """Best-effort extract of KEY: value work-order lines from the prompt.

    The in-process impl receives the composed prompt (not a params dict), so for
    the demo we parse the work order the node built. A real agent would just use
    inv.prompt as its input text.
    """
    out: dict[str, str] = {}
    for line in inv.prompt.splitlines():
        if ": " in line and not line.startswith("#"):
            k, _, v = line.partition(": ")
            out[k.strip().lower()] = v.strip()
    return out


# --- The pipeline as data ---------------------------------------------------
# Both nodes are in-process (impl_ref set).
#
# Three orthogonal names per node:
#   name=      the NODE identity in the DAG (depends_on target, result key,
#              --only/--start-from target). No registry lookup.
#   agent=     the AGENT identity — label shown in the results table. Also the
#              key used to look up a @registry.mock_agent in --mock-agents mode.
#              For subprocess nodes it is the --agent name (the .md file).
#   impl_ref=  the key used to look up @registry.agent_impl in the registry
#              (separate dict from mock_agent). Does NOT need to match `agent`
#              or `name` — it just must match the string passed to agent_impl().
#
# Keeping name == agent == impl_ref (as done here) is the simplest convention
# and avoids confusion; diverge only when there is a real reason to.
#
# classify publishes its typed fields downstream via `exports` so respond can
# template {category}/{urgency} into its work order.
FLOW = FlowDef(
    name="ticket-triage (in-process)",
    nodes=[
        NodeDef(
            name="classify",
            agent="classify",  # matches @REGISTRY.agent_impl("classify")
            impl_ref="classify",  # resolved from registry by this key
            inputs={"TICKET": "{ticket}"},
            result_schema="Classification",
            exports={"category": "category", "urgency": "urgency"},
            gate="capture",
        ),
        NodeDef(
            name="respond",
            agent="respond",  # matches @REGISTRY.agent_impl("respond")
            impl_ref="respond",
            depends_on=["classify"],
            inputs={"CATEGORY": "{category}", "URGENCY": "{urgency}"},
            result_schema="DraftReply",
            gate="capture",
        ),
    ],
)


# A capture gate: reads each node's VALIDATED typed object (ctx.obj) and stashes
# it so we can print the typed results after the run. A gate always returns a
# Directive; this one just observes and Continues. It proves the in-process path
# surfaces `ctx.obj` exactly like the subprocess path.
CAPTURED: dict[str, object] = {}


@REGISTRY.gate("capture")
def capture(ctx):  # noqa: ANN001
    from agent_flow.gates import Continue

    node_name = getattr(ctx.node, "name", None) or str(ctx.node)
    CAPTURED[node_name] = ctx.obj
    return Continue()


def main(ticket: str = "the app crashes on login, urgent") -> None:
    """Run the two in-process agents via run_flow and print their typed results.

    We run PROGRAMMATICALLY (run_flow), not via run_cli, because the CLI's
    pre-flight is currently subprocess-oriented (it defaults runtime=opencode and
    requires an agent-dir) and would abort an all-in-process flow. See issue #11.
    run_flow skips that gate — nothing here spawns a process.
    """
    from agent_flow import run_flow

    CAPTURED.clear()
    result = run_flow(FLOW, registry=REGISTRY, ticket=ticket)

    print(f"\nticket: {ticket!r}\n")
    for name in ("classify", "respond"):
        print(f"  [{name}] {result[name].status} -> {CAPTURED.get(name)!r}")


if __name__ == "__main__":
    import sys

    main(sys.argv[1] if len(sys.argv) > 1 else "the app crashes on login, urgent")
