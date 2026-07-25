"""Flow-control gates — the orchestration-layer decision point after an agent run.

The engine (`run_agent`) supervises exactly ONE subprocess and returns an
`AgentResult`; it knows nothing about nodes, artifacts, or the DAG. Everything
that is a FLOW decision — "the report file is missing, re-run this node",
"resume the flow at an earlier node", "this blocking node failed, stop" — is
expressed here, as a per-node GATE. The gate is the CONSUMER's optional hook.

A gate is any callable `(GateContext) -> Directive`. The engine invokes it after
the agent completes and acts on the returned directive. A node with NO gate
behaves as if the gate returned `Continue()` — the default, absent-means-continue
rule.

This is the seam that keeps domain knowledge (what a report is, when a re-run is
needed) out of the engine: a gate is free to stat files, read the control dict,
inspect telemetry — whatever it needs — and translate that into one of four
directives.

Directives:

    Continue()          proceed to the next node (also the default / gate absent)
    Restart()           re-run THIS node's agent (bounded by max_cycles)
    GoTo(node)          resume the flow at a named node (re-run loops, jump-back)
    Stop(reason)        abort the whole pipeline (e.g. blocking-criticality failure)
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class Continue:
    """Proceed to the next node. The default when a node has no gate."""


@dataclass(frozen=True)
class Restart:
    """Re-run this node's agent. Bounded by the runner's max-cycles guard.

    note: optional context threaded into the re-run prompt (e.g. why).
    """

    note: str = ""


@dataclass(frozen=True)
class GoTo:
    """Resume the flow at a named node — the general 'continue at a node'.

    Covers the analyst/verifier re-run loop (goto the analyst's node) and the
    consistency-check jump-back. Bounded by the runner's max-cycles guard to
    prevent loops.

    note: optional context threaded into the target node's prompt.
    """

    node: str
    note: str = ""


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
    run_dir   the run's directory — a gate stats files under here to
              check what the agent wrote.
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
    run_dir: Path
    cycles: int
    obj: Any = None
    params: dict[str, Any] = field(default_factory=dict)
    agent_dir: str = ""


# A gate is any callable from context to directive. `None` == always Continue.
Gate = Callable[[GateContext], Directive]


# Ready-made gates — the two checks almost every pipeline writes, shipped so the
# consumer doesn't hand-roll a closure. Both are fully OPTIONAL and composable;
# they are ordinary gates a consumer may use, wrap, or ignore.


def require_file(ctx: GateContext, *, relpath: str, on_missing: Directive | None = None) -> Directive:
    """Gate: require a (non-empty) file under run_dir; else re-run the node.

    A gate is `(ctx, **config) -> Directive`. Config here: `relpath` (the file to
    require) and optional `on_missing`. `relpath` may template run params via
    `{name}` (e.g. "{run_dir}/{product_key}.md"), resolved against `ctx.params` —
    the SAME params the node's `run` saw — so a run-time-dependent path still
    resolves. File present -> Continue; missing -> `on_missing` (default Restart,
    i.e. give the agent another bounded try).

    Referenced from a node as gate="require_file", gate_args={"relpath": "..."}.
    """
    from agent_flow.core import produced

    rel = _resolve_relpath(relpath, ctx)
    if produced(ctx.run_dir / rel):
        return Continue()
    return on_missing if on_missing is not None else Restart(note=f"required file missing: {rel}")


def rerun_on_signal(ctx: GateContext, *, target: str, control_file: str | None = None) -> Directive:
    """Gate: re-run a FIXED `target` node when the sidecar asks for a re-run.

    Config: `target` (which node to jump back to) and optional `control_file`.
    Reads `rerun_required` from the current node's control sidecar (default
    `<node>.control.json`); if non-empty -> GoTo(target) (bounded by the walker),
    else Continue. The named values are only a truthy signal; the destination is
    the fixed `target`. The common verifier case (always bounces to its one
    subject). For a variable destination, use `rerun_on_named`.

    Referenced as gate="rerun_on_signal", gate_args={"target": "..."}.
    """
    from agent_flow.core import rerun_from_sidecar

    node_name = getattr(ctx.node, "name", None) or str(ctx.node)
    cf = control_file or f"{node_name}.control.json"
    if rerun_from_sidecar(ctx.run_dir / cf):
        return GoTo(node=target, note=f"{node_name} signalled re-run of {target}")
    return Continue()


def rerun_on_named(ctx: GateContext, *, control_file: str | None = None) -> Directive:
    """Gate: re-run WHICHEVER node the sidecar's `rerun_required` names.

    Unlike `rerun_on_signal` (fixed target), routes to the node named in the
    sidecar — for a final coherence check that may bounce to any upstream stage.
    `rerun_required` carries NODE names; the FIRST valid backward target is used
    (the walker bounds/validates the jump). Empty -> Continue.

    Referenced as gate="rerun_on_named" (no required args).
    """
    from agent_flow.core import rerun_from_sidecar

    node_name = getattr(ctx.node, "name", None) or str(ctx.node)
    cf = control_file or f"{node_name}.control.json"
    named = rerun_from_sidecar(ctx.run_dir / cf)
    if named:
        target = named[0]
        return GoTo(node=target, note=f"{node_name} signalled re-run of {target}")
    return Continue()


def _resolve_relpath(relpath: str, ctx: GateContext) -> str:
    """Best-effort `{name}` expansion for a gate's file path, from run params.

    Exposes `{run_dir}` too (like agent_node's input templating), so the SAME
    path template works whether written as "report.md" or "{run_dir}/report.md":
    a bare relative path is joined onto ctx.run_dir by the caller, and an
    absolute "{run_dir}/…" path collapses to the same location (Path join drops
    the run_dir prefix when the right-hand side is already absolute).

    Uses `ctx.params` (the same run-time values a node's `run` sees) rather than
    the node's static declaration, since only params are known at gate-eval time.
    A missing placeholder is left literal (no KeyError) so an unrelated `{...}`
    in the path does not crash the gate.
    """
    tmpl = {**ctx.params, "run_dir": str(ctx.run_dir)}
    try:
        return relpath.format(**tmpl)
    except KeyError, IndexError:
        return relpath
