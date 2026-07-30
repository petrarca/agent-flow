"""The WORK ORDER — the KEY: value payload handed to an agent, and its renderers.

`inputs` is how a flow passes an agent what it needs. This module resolves those
values against the run's params, validates them against an optional input
schema, and renders them into the prompt body. Two renderers ship (XML tags and
plain lines); a consumer may register its own.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from agent_flow.protocol import coerce_schema
from agent_flow.utils import resolve_template


def _validate_inputs(node: str, input_schema: object, resolved: dict[str, str]) -> object | None:
    """Validate a node's RESOLVED work order against its `input_schema`.

    The mirror of the result-schema check, on the way IN. Runs after templating,
    so an unresolved `{param}`/`{export}` surfaces as a real schema error here —
    before an agent is spawned — instead of reaching the agent as the literal
    text "{mode}". Returns the typed instance (a pydantic model) for an in-process
    impl to use, or None when no schema is declared (or a plain dict JSON-schema
    was used, which validates but yields no new object).

    Raises ValueError on invalid input; `interpret` maps that through the node's
    criticality like any other node failure (blocking halts, degrade degrades).
    """
    if input_schema is None:
        return None

    schema = coerce_schema(input_schema)
    if schema is None:
        return None
    outcome = schema.validate(resolved)
    if not outcome.valid:
        raise ValueError(f"node {node!r}: inputs do not match input_schema: {'; '.join(outcome.errors)}")
    return outcome.obj


def resolve_work_order(inputs: dict[str, str], params: dict[str, Any]) -> dict[str, str]:
    """Resolve `{param}` templates in every input value; return the structured dict.

    This is the single place where input values are resolved. Both the prompt
    representation (KEY: value lines) and the structured MockAgentContext.input()
    dict are derived from it.
    """
    return {key: resolve_template(val, params) for key, val in inputs.items()}


# A work-order RENDERER turns the resolved `{KEY: value}` work order into the
# prompt text an agent sees. It is a seam: the default is XML, and a consumer may
# pass any callable (per node, or flow-wide) to control the shape entirely.
WorkOrderRenderer = Callable[[dict[str, str]], str]


def render_work_order_xml(resolved: dict[str, str]) -> str:
    """Render the work order as XML-ish tags — the DEFAULT.

        <PRODUCT_KEY>acme</PRODUCT_KEY>
        <REPORT>/run/report.md</REPORT>

    Why this and not `KEY: value`: a closing tag DELIMITS the value, so a
    multi-line or structured value is unambiguous (a line-oriented work order has
    no continuation marker, so its second line is indistinguishable from the next
    key). Tags are also the shape Anthropic recommends for Claude prompts, and an
    agent resolves `<REPORT>` without being told anything about the format — the
    instructions in an agent's .md refer to the KEY name, which is unchanged.

    A multi-line value is placed on its own lines so both the value and the
    surrounding tags stay readable. Values are NOT XML-escaped: this is prompt
    text for a model, not a document for a parser, and escaping would only make
    it harder to read.
    """
    parts: list[str] = []
    for key, val in resolved.items():
        if "\n" in val:
            parts.append(f"<{key}>\n{val}\n</{key}>")
        else:
            parts.append(f"<{key}>{val}</{key}>")
    return "\n".join(parts)


def render_work_order_lines(resolved: dict[str, str]) -> str:
    """Render the work order as `KEY: value` lines — the pre-0.3 shape.

        PRODUCT_KEY: acme
        REPORT: /run/report.md

    Kept as a shipped renderer so a pipeline tuned on this shape can opt back in
    (`build_flow(work_order_renderer=render_work_order_lines)`). Note a value
    containing a newline is ambiguous here — that is precisely what the XML
    default fixes.
    """
    return "\n".join(f"{key}: {val}" for key, val in resolved.items())


DEFAULT_WORK_ORDER_RENDERER: WorkOrderRenderer = render_work_order_xml


def build_work_order(inputs: dict[str, str], params: dict[str, Any], *, render: WorkOrderRenderer | None = None) -> str:
    """Resolve `{param}` templates in `inputs` and render the work-order prompt.

    Each value may reference run params via `{name}` (e.g. "{product_key}"); the
    library exposes nothing implicitly — the consumer decides the keys. The
    completion protocol (CONTROL_FILE + control JSON shape) is injected separately
    by the executor, so it is NOT part of these inputs.

    `render` selects the shape (default: `render_work_order_xml`).
    """
    resolved = resolve_work_order(inputs, params)
    return (render or DEFAULT_WORK_ORDER_RENDERER)(resolved)
