"""The `flow` command group — introspect the pipeline's flow / graph.

`flow nodes` prints the pipeline's nodes in EXECUTION ORDER (via plan_groups),
so you can discover node/group names to pass to `run --only` / `run --start-from`
and see which node runs which agent. `flow` is the info-about-the-flow group
(`run` stays the top-level execute verb); it can grow (`flow show`, `flow export`).

--json is intentionally not provided yet — see petrarca/agent-flow#9 (the
dataclass-vs-pydantic modeling decision it depends on).
"""

from __future__ import annotations

from agent_flow.cli.console import get_console
from agent_flow.cli.context import RunCliContext


def register(app, ctx: RunCliContext) -> None:
    """Attach the `flow` sub-group (with `nodes`) to `app`."""
    import typer

    flow_app = typer.Typer(no_args_is_help=True, help="Inspect the pipeline's flow / graph.")

    @flow_app.command("nodes")
    def nodes(
        with_details: bool = typer.Option(False, "--with-details", help="show extra columns (criticality, max cycles, gate, schema, exports)"),
    ) -> None:
        """List the pipeline's nodes in execution order."""
        _print_nodes_table(ctx.build_nodes(), ctx.name, details=with_details, console=get_console())

    app.add_typer(flow_app, name="flow")


def _print_nodes_table(nodes, name: str, *, details: bool, console) -> None:
    """Render the nodes as a rich table in execution order.

    Groups come from plan_groups (dependency-respecting order; parallel-group
    members share a group index). The compact table shows the essentials; -d adds
    the per-node knobs. A gate marks a node that can steer the flow (re-run /
    jump-back) — the state-machine transitions on top of the DAG edges.
    """
    from rich.table import Table

    from agent_flow.engine import plan_groups

    planned = plan_groups(nodes)  # [(group_key, [Node]), ...] in execution order

    table = Table(title=f"{name} — nodes (execution order)", title_style="bold")
    table.add_column("#", justify="right", style="dim")  # group index (parallel members share it)
    table.add_column("Node")
    table.add_column("Agent", style="dim")
    table.add_column("Group")  # parallel_group, or "-" for solo
    table.add_column("Depends on", style="dim")
    if details:
        table.add_column("Criticality")
        table.add_column("Max cycles", justify="right")
        table.add_column("Gate")
        table.add_column("Schema")
        table.add_column("Exports")

    for idx, (group_key, group) in enumerate(planned):
        parallel = len(group) > 1
        for n in group:
            row = [
                str(idx),
                n.name,
                n.agent or "-",
                group_key if parallel else "-",
                ", ".join(n.depends_on) or "-",
            ]
            if details:
                row += [
                    n.criticality,
                    str(n.max_cycles),
                    (n.gate_ref if n.gate_ref else "yes") if (n.gate_ref or n.gate is not None) else "-",
                    "yes" if n.result_schema is not None else "-",
                    (n.export_ref if n.export_ref else "yes") if (n.export_ref or n.exports is not None) else "-",
                ]
            table.add_row(*row)

    console.print(table)
