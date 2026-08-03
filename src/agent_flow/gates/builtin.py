"""The gates that ship with the library.

Each is a thin composition of a signal (`gates.signals`) and a directive: check
something about what the agent produced, return the directive that follows. A
consumer's own gate is written the same way.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from upath import UPath

from agent_flow.gates.signals import produced, read_field
from agent_flow.gates.types import Continue, Directive, GateContext, Restart, Stop

# Ready-made gates — the checks almost every pipeline writes, shipped so the
# consumer doesn't hand-roll a closure. All are fully OPTIONAL and composable;
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
    resolved = _resolve_path(path, ctx)
    if produced(_join_run_dir(ctx.run_dir, resolved)):
        return Continue()
    return on_missing if on_missing is not None else Restart(instruction=f"The required file is missing: {resolved}. Produce it.")


def stop_if(ctx: GateContext, *, field: str, equals: Any, reason_field: str = "reason", label: str = "") -> Directive:
    """Gate: STOP the run when the agent's result `field` equals a sentinel value.

    The deterministic "precondition failed, abort" check — e.g. a readiness node
    whose result carries `ready: 'yes'|'no'|'partial'`, stopping the run on 'no'.
    Config:
      field         the result field to test (required).
      equals        the sentinel value that TRIPS the stop (required); the run
                    aborts when `field == equals`, otherwise it continues.
      reason_field  a result field whose value becomes the Stop reason detail
                    (default "reason"); ignored when absent/empty.
      label         optional prefix for the Stop reason, naming the pipeline/stage
                    (e.g. "tech-assessment") so the operator sees where it stopped.

    Reads the VALIDATED typed object (`ctx.obj`) when the node set a
    `result_schema`, else the raw envelope (`ctx.result`) — so a field lookup
    works whether the pipeline typed its result or not. Match -> Stop; else
    Continue.

    Referenced as gate="stop_if", gate_args={"field": "ready", "equals": "no"}.
    """
    if read_field(ctx, field) == equals:
        detail = read_field(ctx, reason_field) or f"{field} == {equals!r}"
        return Stop(reason=f"{label}: {detail}" if label else str(detail))
    return Continue()


def _join_run_dir(run_dir: Path | UPath, resolved: str) -> Path | UPath:
    """Turn a resolved gate path into a path object, relative to run_dir if bare.

    An ABSOLUTE resolved path — `/…`, or a scheme-qualified `memory://…` (an
    anchored `{product_repos_root}/…` artifact, or an in-memory run) — stands on
    its own. Only a BARE relative name is joined onto `run_dir`.

    This must NOT be a plain `run_dir / resolved`: pathlib discards the left side
    when the right is absolute, but a `UPath` over `memory://` does not — it would
    concatenate run_dir and the memory URL into a nonsense path. `UPath` decides
    both questions (is it absolute, and which filesystem), and returns an ordinary
    `Path` subclass for a local path — so local and in-memory behave identically.
    """
    target = UPath(resolved)
    return target if target.is_absolute() else run_dir / resolved


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
