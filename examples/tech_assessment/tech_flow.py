"""Thin Layer-3 demo: the simplified tech-assessment DAG on the library engine.

Declaration-driven, batteries-included. Each domain `Stage` (tech_stages.py)
maps to library `Node`s built with `agent_node(...)` — one call per agent, no
hand-written prompt/sidecar/run glue. A "verifier" is NOT a library concept: it
is simply ANOTHER `agent_node` that depends on the node it checks and carries a
`rerun_on_signal(...)` gate; if it flags a re-run, the engine jumps the flow back
to that node (bounded). "analyst"/"verifier" are this example's DOMAIN words —
the library only sees nodes, edges, and gates.

WHERE THE COMPLEXITY LIVES (and why it is optional):
The ONLY library calls needed to build a pipeline are `agent_node(...)` and
`build_flow(...)` (the latter is invoked for us by `run_cli`). The `Stage`
table and its `_nodes_for` compiler below are NOT required by the library —
they are this example's own convenience for a repeating analyst+verifier shape.
A minimal pipeline is just a handful of direct `agent_node(...)` calls (see
examples/toy_pipeline). Read this file as "one WAY to organize nodes", not as
"the API you must use".

Everything structural — DAG ordering, the parallel group, bounded re-runs and
cross-node jump-back, per-node criticality, LLM concurrency, prompt/sidecar
plumbing — is the LIBRARY's job. This file only declares the graph.

Run (the CLI is the library's reusable run_cli — generic flags + -p/--param):
  uv run --with prefect python -m examples.tech_assessment.tech_flow \
      -p product_key=my-product -p repos_root=/tmp/repos --runtime mock
  # or with a config file:
  uv run --with prefect python -m examples.tech_assessment.tech_flow --config run.yml
"""

from __future__ import annotations

# Load .env into os.environ FIRST — before bootstrap() (so the file may set
# PREFECT_API_URL / PREFECT_PERSIST) and before any subprocess is spawned.
from agent_flow.env import load_env

load_env()

from agent_flow._prefect_env import bootstrap  # noqa: E402

bootstrap()

from pathlib import Path  # noqa: E402

from agent_flow import agent_node  # noqa: E402
from agent_flow.engine import Node  # noqa: E402
from agent_flow.gates import require_file, rerun_on_signal  # noqa: E402
from examples.tech_assessment.tech_stages import STAGES, Stage  # noqa: E402

# This example's own opencode project dir (holds .opencode/agent/*.md).
_PACKAGE_DIR = str(Path(__file__).resolve().parent)

LLM_TAG = "llm"


def _tech_stack_schema():
    """Demo of TYPED agent output on the tech-stack node (optional, consumer-owned).

    Prefer a Pydantic model when the extra is present; else a plain JSON-schema
    dict — both drive the same seam. Only attached to the tech-stack analyst.
    """
    try:
        from pydantic import BaseModel
    except ImportError:
        return {
            "type": "object",
            "properties": {"summary": {"type": "string"}, "languages": {"type": "array", "items": {"type": "string"}}},
            "required": ["summary", "languages"],
        }

    from agent_flow.schema_pydantic import PydanticSchema

    class TechStackResult(BaseModel):
        summary: str
        languages: list[str]

    return PydanticSchema(TechStackResult)


_SCHEMAS = {"tech-stack": _tech_stack_schema()}


def _nodes_for(stage: Stage) -> list[Node]:
    """Compile one domain `Stage` into library `Node`s.

    THIS is the whole reason `Stage` exists — it is the "compiler" that turns
    the compact domain table into the library's real vocabulary. It does three
    things a raw `agent_node(...)` call would make you repeat by hand for every
    stage, which is exactly the boilerplate the table lets us DRY up:

      1. Fan-out: a stage WITH a verifier becomes TWO nodes (analyst + verifier),
         not one. That analyst+verifier pairing is a domain pattern, not a
         library feature — here is where we express it.
      2. Uniform policy: verifiers are ALWAYS `degrade` (a failed check must not
         kill the run) and ALWAYS carry a `rerun_on_signal` gate back to their
         analyst; analysts ALWAYS carry a `require_file` gate ("reported ok but
         wrote nothing -> one retry"). Decided once, applied to every stage.
      3. Convention: the standard `inputs` shape and the `<name>.md` report path
         are filled in from the stage, so the table stays terse.

    If you did NOT want this pairing/policy sugar, you would skip `Stage` and
    call `agent_node(...)` directly for each node instead of this function.
    """
    # Per-node instruction (a): additive guidance for THIS node only. Shown on
    # the tech-stack analyst as a demo; run-wide brief (b) comes via the CLI.
    per_node = "List concrete versions where known; prefer a compact table." if stage.name == "tech-stack" else ""
    analyst = agent_node(
        name=stage.name,
        agent=stage.analyst,
        inputs={"PRODUCT_KEY": "{product_key}", "PRODUCT_REPOS_ROOT": "{repos_root}", "REPORT": "{run_dir}/" + stage.report},
        instructions=per_node,
        depends_on=stage.depends_on,
        parallel_group=stage.parallel_group,
        criticality=stage.criticality,
        result_schema=_SCHEMAS.get(stage.name),
        # If the agent reports ok but wrote no report, give it one more try.
        gate=require_file(stage.report),
    )
    if not stage.verifier:
        return [analyst]

    verifier = agent_node(
        name=f"{stage.name}-verify",
        agent=stage.verifier,
        inputs={"PRODUCT_KEY": "{product_key}", "REPORT": "{run_dir}/" + stage.report},
        depends_on=(stage.name,),
        criticality="degrade",  # a failed verification should not stop the pipeline
        # If the verifier signals a re-run, jump the flow back to the analyst node.
        gate=rerun_on_signal(target=stage.name),
    )
    return [analyst, verifier]


def build_tech_nodes() -> list[Node]:
    """The whole pipeline as data: flatten every Stage into its library nodes."""
    return [node for stage in STAGES for node in _nodes_for(stage)]


def main() -> None:
    # The whole CLI is the library's reusable runner: generic flags + --config +
    # -p/--param for domain values. This example adds NO bespoke CLI — a run is:
    #   ... tech_flow -p product_key=my-product -p repos_root=/tmp/repos --runtime mock
    # or a config file:  ... tech_flow --config run.yml
    # Domain params (product_key, repos_root) are referenced in the node inputs;
    # the library attaches no meaning to them.
    from agent_flow.cli import run_cli

    run_cli(build_tech_nodes, name="tech-assessment-simplified", llm_tag=LLM_TAG, default_agent_dir=_PACKAGE_DIR)


if __name__ == "__main__":
    main()
