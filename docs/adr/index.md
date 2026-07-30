---
type: ADR
title: Architecture Decision Records
description: Index of agent-flow's decision records — the why behind each direction change.
tags: [agent-flow, adr, architecture, decisions]
status: stable
generated: { by: human:wmi, at: 2026-07-30T00:00:00Z }
---

# Architecture Decision Records

A decision record says *why* a direction was taken. The design bundle
(`../design/`) says what the thing IS; an ADR says what was decided and what was
given up for it.

Numbered `NNNN-title.md`, kebab-case, sequential. An ADR is immutable once
accepted: to change direction, add a new one and set `superseded_by` on the old.

| ADR | Title | Decision |
|---|---|---|
| [0001](0001-layered-architecture-enforced-by-import-linter.md) | Layered architecture, enforced by import-linter in the test suite | accepted |
| [0002](0002-protocol-package-as-shared-leaf.md) | The library↔agent agreement lives in a leaf package below core and runners | accepted |
| [0003](0003-run-scoped-registry-on-the-run-context.md) | The FlowRegistry is run-scoped and reaches a node on the RunContext | accepted |
| [0004](0004-lazy-public-facade.md) | The public facade resolves lazily (PEP 562) | accepted |
