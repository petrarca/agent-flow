"""Simplified tech-assessment DAG as data.

A trimmed model of a tech-assessment orchestrator that keeps every
STRUCTURAL SHAPE that matters, without every stage:

  readiness ─► tech-stack (analyst+verifier, re-run loop)
                   │  (parallel group: 2 analysts, independent)
                   ├─► domain   (analyst+verifier)
                   └─► coupling (analyst+verifier)
                   ▼
              architecture (analyst)          [degrade on failure]
                   ▼
              summary (analyst+verifier)

Sequencing is explicit (`depends_on`) — the engine never infers order from
artifacts. What each agent READS or PRODUCES is the agent's own business,
expressed in its instruction (.md); the flow only passes run-specific INPUTS
(product key, report path, …) into the prompt. Any decision that depends on
what an agent wrote — "no report, re-run" or a verifier's re-run request — is a
GATE the flow invokes after the agent runs (see tech_flow.py). The engine stays
artifact-agnostic; artifact knowledge lives in the gates and the agent .md.

WHY a `Stage` dataclass at all — is it required? NO.
--------------------------------------------------------------------
The LIBRARY only knows `Node` (agent_flow.engine). `Stage` is NOT a library
type and agent_flow never sees it. It is OPTIONAL, example-specific sugar that
exists purely because THIS pipeline has a repeating shape:

    every unit of work = an analyst + its verifier, sharing the same input
    convention, the same re-run gate, and a `<name>.md` report.

Declaring that shape once as data (below) and expanding it into the library's
real vocabulary (`Node` + gates) in ONE place (`_nodes_for` in tech_flow.py) is
just DRY: 5 stages that would otherwise be ~10 near-identical `agent_node`
calls become 5 short rows. It adds ZERO capability — the engine can do all of
this without `Stage`.

If your pipeline is small or irregular (no verifier pattern), SKIP this file
entirely and call `agent_node(...)` directly — see examples/toy_pipeline, which
uses no `Stage` layer at all. Reach for a table like this only when the
repetition actually hurts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Criticality = Literal["blocking", "degrade"]


@dataclass(frozen=True)
class Stage:
    """One row of THIS example's pipeline table (a domain concept, not a library
    type). `_nodes_for` in tech_flow.py compiles each Stage into one or two
    library `Node`s. Fields are either sugar (S) unique to this table, or a
    straight pass-through (P) that maps 1:1 to a library `Node` field.

    name          (P) stage id -> the analyst node's name.
    analyst       (S) agent that does the work. Sugar: becomes node `agent=`.
    verifier      (S) optional paired verifier. Sugar: its PRESENCE is what makes
                      one Stage expand into TWO nodes (analyst + verifier) with a
                      re-run gate wired between them. The library has no
                      "verifier" concept — a verifier is just another node.
    depends_on    (P) upstream stage names that must finish first (DAG edges).
    parallel_group (P) stages sharing a group name fan out concurrently.
    criticality   (P) 'blocking' -> failure STOPS the pipeline;
                      'degrade'  -> failure is logged, pipeline continues.
    inputs        (S) run-specific KEY: value pairs injected into the agent
                      prompt. This is the ONLY thing the flow knows about
                      artifacts — it passes 'REPORT' as an input; what the agent
                      does with it is in the .md. An agent that produces nothing
                      simply gets different (or no) inputs.
    model         (S) optional per-stage model override.
    idle_timeout_s (S) liveness budget: kill the agent only after this many
                      seconds with NO event and NO sidecar (no absolute cap).
                      Real opencode agents can pause 60-90s between tool calls.
    """

    name: str
    analyst: str
    verifier: str | None = None
    depends_on: tuple[str, ...] = ()
    parallel_group: str | None = None
    criticality: Criticality = "blocking"
    inputs: dict[str, str] = field(default_factory=dict)
    model: str | None = None
    idle_timeout_s: int = 120  # generous for real LLM agents; mock exits instantly

    @property
    def report(self) -> str:
        """Convenience: the REPORT input, defaulting to '<name>.md'.

        This is just a helper for building prompts and for gates that check
        'was the report written' — it is NOT an engine contract.
        """
        return self.inputs.get("REPORT", f"{self.name}.md")


# The DAG. Sequencing is explicit via depends_on; parallel_group members are
# dispatched together.
STAGES: list[Stage] = [
    Stage(
        name="tech-stack",
        analyst="tech-stack-analyst",
        verifier="tech-stack-verifier",
        criticality="blocking",
        inputs={"REPORT": "tech-stack.md"},
    ),
    Stage(
        name="domain",
        analyst="domain-analyst",
        verifier="domain-verifier",
        depends_on=("tech-stack",),
        parallel_group="analysis",
        criticality="degrade",
        inputs={"REPORT": "domain.md"},
    ),
    Stage(
        name="coupling",
        analyst="coupling-analyst",
        verifier="coupling-verifier",
        depends_on=("tech-stack",),
        parallel_group="analysis",
        criticality="degrade",
        inputs={"REPORT": "coupling.md"},
    ),
    Stage(
        name="architecture",
        analyst="architecture-analyst",
        verifier=None,
        depends_on=("domain", "coupling"),
        criticality="degrade",
        inputs={"REPORT": "architecture.md"},
    ),
    Stage(
        name="summary",
        analyst="executive-summary",
        verifier="summary-verifier",
        depends_on=("architecture",),
        criticality="degrade",
        inputs={"REPORT": "summary.md"},
    ),
]
