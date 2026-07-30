---
type: ADR
title: Layered architecture, enforced by import-linter in the test suite
description: The tier rule becomes a build-failing contract checked by import-linter, run from the unit tests rather than as a separate verify step.
tags: [agent-flow, architecture, layering, fitness-function, import-linter]
decision: accepted
status: stable
supersedes: []
superseded_by: []
generated: { by: human:wmi, at: 2026-07-30T00:00:00Z }
---

# Layered architecture, enforced by import-linter in the test suite

> The layering was a convention that documentation asserted and nothing checked;
> it is now a contract that fails the test run.

## Context

The library states a tier rule: Tier 3 (the engine) must not import Tier 1
(`core` / `runners`) — they meet only through a node's `run` callable. Two guards
existed. One blocks `import prefect` and proves the core is backend-free. The
other walks the AST and fails on a module-level import cycle.

Neither asserted dependency *direction*, and the cycle guard inspects
module-level imports only — correct for what it claims, since only those deadlock
at package init. Three package cycles (`core ↔ runners`, `engine ↔ backends`,
`preflight ↔ runners`) were each broken by a function-local import, so the guard
passed on all of them by construction. A fourth, `schema ↔ schema_pydantic`, was
never noticed at all. The two instruments available for reasoning about
structure — the guard and the AST index — both reported a cleaner layering than
existed.

That mattered beyond tidiness. The next planned feature, `ServeExecutor`, lands
in the factory holding one side of the `core ↔ runners` cycle.

## Decision

We will express the architecture as import contracts in `pyproject.toml` under
`[tool.importlinter]`: layer order, the tier rule, the leaf packages (`gates`,
`protocol`), backends-not-engine, and acyclicity.

We will run them from the unit test suite (`tests/unit/test_layering.py`), and
**not** additionally from `task verify` — one place only.

*Why import-linter rather than our own graph walk.* We surveyed the Python
options against ArchUnit-style alternatives. import-linter is the de-facto
standard by adoption, actively maintained, and runs locally with no network
access. Decisively, its graph engine sees function-local imports — the exact
blind spot that hid the four cycles — and it ships the contract types we would
otherwise hand-roll and maintain ourselves.

*Why the test suite rather than a separate stage.* Cycles and layering are
"atomic, triggered" fitness functions in the Building Evolutionary Architectures
taxonomy, the category that maps to unit tests. ArchUnit, the reference
implementation of the idea, is deliberately tests rather than a linter so
violations surface in the developer's own loop; `task fct` is that loop here.
The usual argument for a separate stage is analysis cost, which does not apply
at the measured runtime.

## Consequences

A violation now fails where the change is made. Adding a top-level package fails
until it is placed in the layer contract deliberately, so the architecture cannot
be extended by accident.

The contracts are declarative and live next to the dependency declarations, and
`lint-imports` still runs standalone for ad-hoc checks.

Accepted trade-offs. Two dev dependencies (`import-linter`, `click`; `rich` was
already present) and a Rust extension in `grimp`, shipped as a prebuilt wheel —
the same model as `pydantic-core` and `ruff`, both already in the stack. The
tool is effectively single-maintainer; the mitigation is that it is small, pinned
and forkable, and its failure mode is stagnation rather than breakage.

Static analysis cannot see an import by computed name, so the contracts model the
real graph only while every dynamic import resolves over a statically declared
set. `test_layering.py` asserts that property directly rather than assuming it.

The contract for the tier rule uses `allow_indirect_imports`, restricting it to
*direct* coupling. The engine does reach Tier 1 transitively through
`node_builder`, which is the nominated bridge; forbidding that would forbid the
bridge itself.
