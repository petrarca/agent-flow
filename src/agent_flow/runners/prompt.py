"""The prompt CHANNELS and the renderer that turns them into a prompt body.

`PromptParts` keeps every channel separate — run-wide context and brief, per-node
context and instructions, the one-time attempt instruction, the work order — so a
consumer's own renderer receives the pieces rather than a finished string. The
control protocol is deliberately NOT a part: it belongs to the runner, which
prepends it after rendering.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class PromptParts:
    """The prompt's channels, UNASSEMBLED — the input to a prompt renderer.

    Every channel the library composes, in scope order, each still separate so a
    renderer decides the wording, headings and order. Fields are "" (or {}) when
    the channel is unused. See docs/design/input-plane.md.

    NOT here: the COMPLETION PROTOCOL. That block is half of the verdict contract
    — the executor injects a sidecar path and then reads back that exact path —
    so it is owned by the runner (`build_verdict_preamble`) and prepended after
    rendering. A prompt renderer cannot break it.

    `work_order` is the already-rendered data block; `inputs` is the same data
    still structured, so a renderer may ignore the rendered form and lay the
    values out itself.
    """

    run_context: str = ""  # [run] ingested FILE CONTENT for every agent
    run_instructions: str = ""  # [run] STANDING brief, declared on the flow
    run_additional_instructions: str = ""  # [run] inline text supplied at RUN time (-i / config instructions)
    node_context: str = ""  # [node] ingested FILE CONTENT for this node
    node_instructions: str = ""  # [node] inline text, declared at build time
    node_runtime_instructions: str = ""  # [node] inline text supplied at RUN time (--instruct)
    attempt_instruction: str = ""  # [attempt] one-time text from a gate's Restart/GoTo
    work_order: str = ""  # [node] the rendered data block
    inputs: dict[str, str] = field(default_factory=dict)  # the same data, structured


def render_prompt(parts: PromptParts) -> str:
    """Assemble the prompt body from its parts — the DEFAULT renderer.

    Order is scope-outward: run -> node -> attempt -> data; and within a scope,
    ingested context precedes inline instructions, so an agent reads the
    authoritative rules first, then guidance, then the concrete task. Empty
    channels are skipped entirely (no stray headings).

    Override wholesale with `FlowRegistry.prompt`; to change only the data block,
    override the inner `FlowRegistry.work_order` instead.
    """
    blocks: list[str] = []
    if parts.run_context.strip():
        blocks.append(f"## Run-wide context\n\n{parts.run_context.strip()}")
    if parts.run_instructions.strip():
        blocks.append(f"## Run-wide instructions\n\n{parts.run_instructions.strip()}")
    if parts.run_additional_instructions.strip():
        blocks.append(f"## Additional run-wide instructions\n\n{parts.run_additional_instructions.strip()}")
    if parts.node_context.strip():
        blocks.append(f"## Context for this step\n\n{parts.node_context.strip()}")
    if parts.node_instructions.strip():
        blocks.append(f"## Instructions for this step\n\n{parts.node_instructions.strip()}")
    if parts.node_runtime_instructions.strip():
        blocks.append(f"## Additional instructions for this run\n\n{parts.node_runtime_instructions.strip()}")
    if parts.attempt_instruction.strip():
        # Verbatim, no library heading: the gate that produced it owns its framing.
        blocks.append(parts.attempt_instruction.strip())
    if parts.work_order:
        blocks.append(parts.work_order)
    return "\n\n".join(blocks)
