# examples

Demonstrations of how to build pipelines on top of the `agent-flow` library.

## tech_assessment/
A simplified model of a tech-assessment DAG (tech-stack → domain ∥
coupling → architecture → summary). Shows sequencing, parallel fan-out,
analyst→verifier re-run loop, and per-stage criticality.

```bash
task example:tech:mock PRODUCT=my-product   # mock agents, no tokens
task example:tech PRODUCT=my-product         # real opencode + model
```

## toy_pipeline/
A minimal 3-stage pipeline (analyst → verifier → extractor). It hand-writes its
own Prefect flow, reusing the library primitives (run_agent, build_run_config,
preflight), and demonstrates transactions, rollback, and resume.

```bash
task example:toy:mock TOPIC="Hexagonal architecture"
task example:toy TOPIC="Hexagonal architecture"
```
