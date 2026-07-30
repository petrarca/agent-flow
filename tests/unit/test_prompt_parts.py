"""Unit tests for the prompt seams — `PromptParts`, `registry.prompt`, `work_order`.

The two seams compose as a PIPELINE: the work-order renderer turns the resolved
inputs into `parts.work_order`, then the prompt renderer assembles the body from
all the parts. The completion protocol is deliberately NOT a part — it belongs to
the runner, so a prompt renderer cannot break the verdict contract.
"""

import tempfile

import anyio
import pytest

from agent_flow import agent_node, build_flow
from agent_flow.node_builder import render_work_order_lines
from agent_flow.registry import FlowRegistry
from agent_flow.runners.prompt import PromptParts, render_prompt


def _prompt_for(registry, **flow_kwargs) -> str:
    seen = {}

    async def impl(inv):
        seen["prompt"] = inv.prompt
        seen["inv"] = inv
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl, instructions="Be brief.", inputs={"PRODUCT": "acme"})
    with tempfile.TemporaryDirectory() as d:
        anyio.run(lambda: build_flow([n], name="w", registry=registry, **flow_kwargs)(run_dir=d))
    return seen["prompt"]


# --- the default renderer ---------------------------------------------------


def test_default_renderer_orders_scope_outward_and_skips_empty_channels():
    parts = PromptParts(
        run_context="RULES",
        run_instructions="BRIEF",
        node_context="NODECTX",
        node_instructions="NODEINSTR",
        node_runtime_instructions="RUNTIME",
        attempt_instruction="ATTEMPT",
        work_order="<K>v</K>",
    )
    body = render_prompt(parts)
    order = [body.index(x) for x in ("RULES", "BRIEF", "NODECTX", "NODEINSTR", "RUNTIME", "ATTEMPT", "<K>v</K>")]
    assert order == sorted(order), "channels must render scope-outward: run -> node -> attempt -> data"
    # an empty channel contributes nothing at all — not even its heading
    assert "## Run-wide context" not in render_prompt(PromptParts(work_order="<K>v</K>"))


def test_attempt_instruction_has_no_library_heading():
    """A gate owns the framing of its one-time instruction, so it goes in verbatim."""
    body = render_prompt(PromptParts(attempt_instruction="REDO the section."))
    assert body == "REDO the section."


# --- the inner seam: work_order --------------------------------------------


def test_work_order_renderer_changes_only_the_data_block():
    reg = FlowRegistry()
    reg.work_order(render_work_order_lines)
    body = _prompt_for(reg)
    assert "PRODUCT: acme" in body  # data restyled
    assert "## Instructions for this step" in body  # everything else untouched


# --- the outer seam: prompt -------------------------------------------------


def test_prompt_renderer_replaces_the_whole_body():
    reg = FlowRegistry()

    @reg.prompt
    def only_the_work_order(parts):
        return f"<work_order>\n{parts.work_order}\n</work_order>"

    body = _prompt_for(reg)
    assert body == "<work_order>\n<PRODUCT>acme</PRODUCT>\n</work_order>"
    assert "## Instructions for this step" not in body  # the default layout is gone


def test_prompt_renderer_receives_every_channel_separately():
    reg = FlowRegistry()
    captured = {}

    @reg.prompt
    def capture(parts):
        captured["parts"] = parts
        return render_prompt(parts)

    _prompt_for(reg, run_instructions="BRIEF")
    p = captured["parts"]
    assert p.run_instructions == "BRIEF"
    assert p.node_instructions == "Be brief."
    assert p.work_order == "<PRODUCT>acme</PRODUCT>"
    assert p.inputs == {"PRODUCT": "acme"}  # the same data, still structured


def test_the_two_seams_compose_as_a_pipeline():
    """Setting BOTH is well defined: work_order renders the data, prompt lays it out."""
    reg = FlowRegistry()
    reg.work_order(render_work_order_lines)

    @reg.prompt
    def wrap(parts):
        return f"<data>\n{parts.work_order}\n</data>"

    assert _prompt_for(reg) == "<data>\nPRODUCT: acme\n</data>"


def test_a_prompt_renderer_can_re_render_from_the_raw_inputs():
    """`parts.inputs` keeps the structured form, so the outer seam is not limited
    to the text the inner one produced."""
    reg = FlowRegistry()

    @reg.prompt
    def from_data(parts):
        import json

        return json.dumps(parts.inputs)

    assert _prompt_for(reg) == '{"PRODUCT": "acme"}'


# --- the invariant ----------------------------------------------------------


def test_completion_protocol_is_not_a_prompt_part():
    """It is half of the verdict contract (the executor injects a sidecar path and
    reads back that exact path), so it is the runner's, not a renderable channel."""
    assert not hasattr(PromptParts(), "completion_protocol")
    reg = FlowRegistry()
    reg.prompt(lambda parts: "TOTALLY REPLACED")
    assert _prompt_for(reg) == "TOTALLY REPLACED"  # the body, and only the body


@pytest.mark.parametrize("seam", ["work_order", "prompt"])
def test_unset_seams_fall_back_to_the_library_defaults(seam):
    reg = FlowRegistry()
    got = reg.get_work_order_renderer() if seam == "work_order" else reg.get_prompt_renderer()
    assert got.__name__ in ("render_work_order_xml", "render_prompt")


# --- the two invocation kinds must not double the run-wide blocks ------------


def test_rendered_invocation_is_not_recomposed():
    """Regression: node_builder renders the FULL body into `prompt`, so
    compose_prompt must return it unchanged. Prepending the run-wide blocks again
    duplicated the rules and the brief in every SUBPROCESS prompt — invisible to
    the unit suite, which reads `inv.prompt` via in-process impls."""
    from agent_flow.runners.invocation import compose_prompt

    seen = {}

    async def impl(inv):
        seen["inv"] = inv
        return {"status": "ok"}

    n = agent_node("n", "a", impl=impl, inputs={"K": "v"})
    with tempfile.TemporaryDirectory() as d:
        rules = f"{d}/rules.md"
        with open(rules, "w") as fh:
            fh.write("RULE-MARKER")
        anyio.run(lambda: build_flow([n], name="t", run_context=[rules], run_instructions="BRIEF-MARKER")(run_dir=d))

    inv = seen["inv"]
    assert inv.parts is not None, "a rendered invocation must carry its parts"
    for marker in ("RULE-MARKER", "BRIEF-MARKER"):
        assert inv.prompt.count(marker) == 1
        assert compose_prompt(inv).count(marker) == 1, f"{marker} duplicated by compose_prompt"


def test_raw_invocation_still_gets_the_run_wide_blocks():
    """Tier 1/2 (`run_agent`) passes its own prompt plus separate run_* fields —
    those must still be prepended."""
    from pathlib import Path

    from agent_flow.runners.invocation import AgentInvocation, compose_prompt

    raw = AgentInvocation(agent="a", prompt="MY-TASK", run_dir=Path("/tmp"), run_context="RULE-MARKER", run_instructions="BRIEF-MARKER")
    assert raw.parts is None
    out = compose_prompt(raw)
    assert out.count("RULE-MARKER") == 1 and out.count("BRIEF-MARKER") == 1
    assert out.index("RULE-MARKER") < out.index("BRIEF-MARKER") < out.index("MY-TASK")
