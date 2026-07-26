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

Restart and GoTo carry an optional one-time `instruction` (plain-text prompt
guidance for the re-run) — distinct from Stop's `reason` (a backward-looking
abort message for the operator/log). See each directive's docstring.
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


def require_file(ctx: GateContext, *, path: str, on_missing: Directive | None = None) -> Directive:
    """Gate: require a (non-empty) file to exist after the node runs; else re-run.

    A gate is `(ctx, **config) -> Directive`. Config:
      path       the file to check. Supports `{param}` templating against the
                 same run params the node saw (e.g. `"{run_dir}/report.md"`,
                 `"{product_repos_root}/{product_key}/report.md"`).
                 A bare filename without a leading `/` or `{run_dir}` (e.g.
                 `"report.md"`) is treated as relative to `run_dir` — it is
                 joined onto `ctx.run_dir`, NOT the process working directory.
                 `run_dir` is the artifact directory for this run (passed as
                 `run_dir=` to `run_flow`/`build_flow`; defaults to a temp dir).
                 Use `"{run_dir}/report.md"` explicitly so `path=` stays
                 consistent with the node's `inputs={"REPORT": ...}` value.
      on_missing optional directive to return when the file is absent (default:
                 Restart with an explanatory instruction).

    File present and non-empty -> Continue; absent or empty -> on_missing.

    Referenced from a node as gate="require_file", gate_args={"path": "..."}.
    """
    from agent_flow.core import produced

    resolved = _resolve_path(path, ctx)
    if produced(ctx.run_dir / resolved):
        return Continue()
    return on_missing if on_missing is not None else Restart(instruction=f"The required file is missing: {resolved}. Produce it.")


def rerun_on_signal(ctx: GateContext, *, target: str, control_file: str | None = None) -> Directive:
    """Gate: re-run a FIXED `target` node when the sidecar asks for a re-run.

    Config:
      target        which node to jump back to (required).
      control_file  path to the control sidecar to read. Bare filename or
                    absolute path. When bare (no leading `/`), resolved relative
                    to `run_dir` (the run artifact dir, NOT the process cwd).
                    Default: `<node-name>.control.json` under `run_dir`.

    Reads `rerun_required` from the sidecar; if non-empty -> GoTo(target)
    (bounded by the walker), else Continue. The common verifier case (always
    bounces to its one fixed subject). For a variable destination use
    `rerun_on_named`.

    Referenced as gate="rerun_on_signal", gate_args={"target": "..."}.
    """
    from agent_flow.core import rerun_from_sidecar

    node_name = getattr(ctx.node, "name", None) or str(ctx.node)
    cf = control_file or f"{node_name}.control.json"
    if rerun_from_sidecar(ctx.run_dir / cf):
        return GoTo(node=target, instruction=f"{node_name} signalled a re-run of {target}.")
    return Continue()


def rerun_on_named(ctx: GateContext, *, control_file: str | None = None) -> Directive:
    """Gate: re-run WHICHEVER node the sidecar's `rerun_required` names.

    Config:
      control_file  path to the control sidecar to read. Bare filename or
                    absolute path. When bare (no leading `/`), resolved relative
                    to `run_dir` (the run artifact dir, NOT the process cwd).
                    Default: `<node-name>.control.json` under `run_dir`.

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
        return GoTo(node=target, instruction=f"{node_name} signalled a re-run of {target}.")
    return Continue()


def _resolve_path(path: str, ctx: GateContext) -> str:
    """Expand `{param}` placeholders in a gate path.

    Template precedence (highest wins):
      1. Node-local inputs (`_inputs` in ctx.result) — the resolved KEY: value
         work-order the node passed its agent (e.g. REPORT, PRODUCT_KEY).
         Most specific: computed for this exact node instance, so they win
         over same-named global params.
      2. Global run params (`ctx.params`) — pipeline-wide values.
      3. `{run_dir}` — always available as the base fallback.

    A bare filename without a leading `/` or `{run_dir}` is joined onto
    ctx.run_dir by the caller — NOT the process cwd.
    Missing placeholders are left literal (no crash).
    """
    node_inputs = ctx.result.get("_inputs", {}) if isinstance(ctx.result, dict) else {}
    # Precedence (highest first): node-local inputs > global params > run_dir.
    # Node inputs are the most specific (computed for this exact node instance)
    # and win over same-named global params. run_dir is always the fallback base.
    tmpl = {"run_dir": str(ctx.run_dir), **ctx.params, **node_inputs}
    try:
        return path.format(**tmpl)
    except KeyError, IndexError:
        return path
