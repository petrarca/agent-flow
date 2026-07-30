"""`agent_node` — one-call construction of a Node that runs a runtime agent.

The Tier-3 <-> Tier-1 bridge. Split by concern:

  work_order.py       the KEY: value payload and its renderers, input validation
  resolve.py          per-node setting precedence (runtime, agent_dir, model,
                      duration/idle) — flow declaration vs run config
  executor_choice.py  which of the four executors runs this node
  builder.py          `agent_node` itself and the node closure
"""

from __future__ import annotations

from agent_flow.node_builder.builder import agent_node, control_path
from agent_flow.node_builder.work_order import (
    DEFAULT_WORK_ORDER_RENDERER,
    WorkOrderRenderer,
    build_work_order,
    render_work_order_lines,
    render_work_order_xml,
    resolve_work_order,
)

__all__ = [
    "DEFAULT_WORK_ORDER_RENDERER",
    "WorkOrderRenderer",
    "agent_node",
    "build_work_order",
    "control_path",
    "render_work_order_lines",
    "render_work_order_xml",
    "resolve_work_order",
]
