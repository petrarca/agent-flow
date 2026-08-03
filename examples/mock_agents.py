"""Mock agent behaviours for the assessment examples (--mock-agents mode).

These are the deterministic, no-token stand-ins for the example's opencode agents.
They live HERE, in the example, not in the library: a mock_agent is flow-supplied
domain behaviour, registered by name on a FlowRegistry. When a flow runs with
`--mock-agents` (mock_agents=True), a node whose `agent` matches a registered name
runs the behaviour below via MockExecutor — no subprocess, no tokens — while the
same flow with `--runtime opencode` runs the real .opencode/agent/*.md agents.

Two reusable behaviours mirror the two roles in the assessment pipeline:
  - `analyst`: writes a short report (keyed by PRODUCT_KEY) and returns a small
    structured result (summary + languages).
  - `verifier`: reads the report, appends a verification note, returns "verified".

`register(registry)` attaches them to every example agent name by role (names
ending in `-verifier` get the verifier; everything else the analyst), so the
examples need only build a registry and call this.
"""

from __future__ import annotations

from agent_flow import AgentInvocation, FlowRegistry
from agent_flow.runners.mock_exec import MockAgentContext

# The example agents, by role. Matches examples/.opencode/agent/*.md.
_ANALYSTS = (
    "tech-stack-analyst",
    "domain-analyst",
    "architecture-analyst",
    "coupling-analyst",
    "executive-summary",
    "analyst",
)
_VERIFIERS = (
    "tech-stack-verifier",
    "domain-verifier",
    "architecture-verifier",
    "coupling-verifier",
    "summary-verifier",
    "verifier",
)


def analyst(inv: AgentInvocation, ctx: MockAgentContext) -> dict:
    """Write a short report keyed by PRODUCT_KEY; return a small structured result.

    Reads the structured work-order inputs (PRODUCT_KEY, REPORT) and writes to the
    REPORT path the node wired. The returned `result` happens to satisfy the
    tech-stack demo schema (summary + languages); stages without a schema ignore
    the extra fields.
    """
    product = ctx.input("PRODUCT_KEY") or "unknown-product"
    report = ctx.input("REPORT") or "{run_dir}/" + f"{inv.agent}.md"
    ctx.write_file(
        report,
        f"# {inv.agent} — {product}\n\n"
        "## Summary\n"
        f"Mock {inv.agent} analysis for product '{product}'.\n\n"
        "## Findings\n"
        f"- Produced by {inv.agent} via the --mock-agents mode (no tokens).\n"
        f"- Product key '{product}' was threaded through the work order.\n",
    )
    return {"status": "ok", "result": {"summary": f"Mock {inv.agent} analysis for '{product}'.", "languages": ["Python", "TypeScript"]}}


def verifier(inv: AgentInvocation, ctx: MockAgentContext) -> dict:
    """Read the REPORT, append a verification note if absent; return 'verified'.

    Re-run signal: set the `MOCK_RERUN_ONCE=<agent-name>` ENV VAR and the first
    call to the verifier whose subject agent matches emits `rerun_required: true`
    (one-time; a marker file prevents a second signal), exercising the bounded
    jump-back loop token-free. `true` is enough — each verifier's node grants
    exactly one target (`rerun_targets=["<subject>"]`), so there is nothing to
    name. Env, not `-p`: a mock reads work-order INPUTS (`ctx.input`) and its
    file tools — never the run PARAMS a `-p` flag sets — so a test-only knob
    rides the env, keeping it out of the real work order.
    """
    import os

    report = ctx.input("REPORT") or "{run_dir}/report.md"
    try:
        text = ctx.read_file(report)
    except FileNotFoundError:
        text = ""

    # One-time re-run signal: emit rerun_required=true on the FIRST call when the
    # MOCK_RERUN_ONCE env var matches this agent's subject. A marker file prevents
    # the signal firing again (bounded loop). `true`, not the subject name — the
    # node granted exactly one target, so there is nothing to choose.
    rerun_target = os.environ.get("MOCK_RERUN_ONCE", "")
    # The verifier's subject is its agent name with "-verifier" stripped, or
    # the RERUN_TARGET input when wired explicitly.
    subject = ctx.input("RERUN_TARGET") or inv.agent.replace("-verifier", "").replace("-verify", "")
    marker = "{run_dir}/.rerun_once_" + inv.agent
    try:
        marker_exists = ctx.read_file(marker) == "1"
    except FileNotFoundError:
        marker_exists = False

    if rerun_target and rerun_target == subject and not marker_exists:
        ctx.write_file(marker, "1")
        return {"status": "verified", "rerun_required": True, "result": {"issues_found": 1}}

    if "## Verification" not in text:
        ctx.write_file(report, text + "\n## Verification\n- Status: verified\n- Issues found: 0\n")
    return {"status": "verified", "result": {"issues_found": 0}}


def register(registry: FlowRegistry) -> FlowRegistry:
    """Register the analyst/verifier behaviours for every example agent name."""
    for name in _ANALYSTS:
        registry.mock_agent(name)(analyst)
    for name in _VERIFIERS:
        registry.mock_agent(name)(verifier)
    return registry
