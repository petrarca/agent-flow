"""The gate VOCABULARY: what a gate receives and what it may return.

A gate is `(GateContext) -> Directive`. The four directives are the whole
flow-control language — Continue, Restart, GoTo, Stop — and the engine
interprets nothing else.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from upath import UPath


class Continue:
    """Proceed to the next node. The default when a node has no gate."""


@dataclass(frozen=True)
class Restart:
    """Re-run this node's agent. Bounded by the runner's max-cycles guard.

    instruction: an optional ONE-TIME instruction for the re-run — plain text
        appended to the re-run prompt as its own block (the freshest, last
        standing guidance before the work order). It is NOT a param: it is
        prompt content, not a `{placeholder}` value. Ephemeral by design — it
        applies to the next attempt only and is cleared once that attempt's
        prompt is built, so it never leaks into a subsequent cycle. Use it to
        tell the agent what to fix on the retry (e.g. "the report is missing the
        Deployment section — add it").
    """

    instruction: str = ""


@dataclass(frozen=True)
class GoTo:
    """Resume the flow at a named node — the general 'continue at a node'.

    Covers the analyst/verifier re-run loop (goto the analyst's node) and the
    consistency-check jump-back. Bounded by the runner's max-cycles guard to
    prevent loops.

    instruction: an optional ONE-TIME instruction for the TARGET node's next run
        — same semantics as Restart.instruction (plain-text prompt block, appended
        last, ephemeral / single-attempt), but delivered to the node we resume at
        rather than this one. Note a GoTo is a RESUME, not inherently a re-run:
        the target may be an earlier node the flow returns to. Use it to steer
        that node's run (e.g. a verifier telling the analyst which finding to fix).
    """

    node: str
    instruction: str = ""


@dataclass(frozen=True)
class Stop:
    """Abort the whole pipeline. Covers blocking-criticality failures."""

    reason: str = ""


# The closed set of directives a gate may return.
Directive = Continue | Restart | GoTo | Stop


@dataclass(frozen=True)
class GateContext:
    """Everything a gate needs to decide the next flow action.

    A gate is the CONSUMER's optional hook: it inspects what the just-finished
    agent produced (its side effects on disk, its control result) and returns a
    Directive to steer the flow. The library supplies this context; what the gate
    checks and decides is entirely the consumer's concern.

    obj       the VALIDATED typed result object when the node declared a
              `result_schema` (a pydantic model instance) — else None. This is the
              clean way to read the agent's structured result: `ctx.obj.ready`
              instead of digging a magic key out of `result`. Prefer it whenever a
              schema is set.
    result    the RAW result envelope — the control dict (status, telemetry, and
              the agent's `result` payload). Use it when there is no schema, or for
              the envelope fields. Typed Any so the library does not dictate shape.
    node      the node that just ran (its name, and whatever the consumer's node
              type carries). Typed Any so the library does not couple to any one
              pipeline's node/stage shape.
    run_dir   the run's directory — a gate stats files under here to check what
              the agent wrote. A local `Path`, or a `UPath` over an in-memory FS
              for a mock run; the same pathlib API either way.
    agent_dir the directory the agent definitions came from (opencode --dir),
              for the just-run node — mirrors RunContext.agent_dir. Usually
              unneeded by a gate (it decides from what the agent produced), but
              provided for symmetry.
    cycles    how many times this node has already been re-run in this run,
              so the gate can enforce its own bound.
    params    the pipeline's run-time params (same dict RunContext.params
              carries), so a gate can resolve a `{name}` template to the SAME
              value the node's run used — e.g. a report path that depends on
              product_key, known only at run time, not at node-declaration time.
    """

    result: Any
    node: Any
    run_dir: Path | UPath
    cycles: int
    obj: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    agent_dir: str = ""


# A gate is any callable from context to directive. `None` == always Continue.
Gate = Callable[[GateContext], Directive]
