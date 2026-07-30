---
type: ADR
title: The public facade resolves lazily (PEP 562)
description: A module-level __getattr__ makes the three-tier design true at import time; importing agent_flow no longer constructs the whole library.
tags: [agent-flow, public-api, imports, performance, pep-562]
decision: accepted
status: stable
supersedes: []
superseded_by: []
generated: { by: human:wmi, at: 2026-07-30T00:00:00Z }
---

# The public facade resolves lazily (PEP 562)

> The three tiers were real at API level and false at import level.

## Context

`agent_flow/__init__.py` imported eighteen modules eagerly to re-export ninety
names. A Tier-1 consumer wanting only `run_agent` therefore constructed the CLI,
both backends, `flowdef`, and every runner module — including `ClaudeCodeRunner`,
which no runtime name resolves to. Nearly every module in the package loaded on
`import agent_flow`.

The optional-dependency boundary held (none of the eighteen pulls `typer`,
`rich`, `prefect`, `yaml` or `dotenv`), so the cost was load time and surface
area rather than installability — but it grows with every runner module added.

## Decision

We will resolve exported names through a module-level `__getattr__` (PEP 562)
backed by a name→module table, caching each resolved name in `globals()` so
subsequent lookups skip the hook. A `TYPE_CHECKING` block keeps the real imports
visible to type checkers and IDEs. Import then loads a handful of modules rather
than the whole package, and neither the CLI nor the backends are among them.

One module stays eager: `logging_setup`, because the loguru library-pattern
`disable()` call needs `LIBRARY_LOGGER` at import time. It is an 85-line leaf.

## Consequences

The tier structure is now true at import time as well as at API level, and the
cost of adding a runner module no longer falls on every consumer.

`__all__` is unchanged at ninety names, every one still resolves, `dir()` matches
it, star-import works, and mypy passes over the whole tree.

Accepted trade-offs. A name that does not exist now raises `AttributeError` at
first *access* rather than `ImportError` at import — code guarding an optional
symbol with `try: from agent_flow import x / except ImportError` would need
adjusting. The name→module table is a second place that must stay in step with
`__all__`; the tests assert they agree. And `import agent_flow` no longer
eagerly validates that every export is importable, so a broken internal module
surfaces on use rather than on import — which the test suite covers.

This does not reduce the ninety-name surface. Auditing those exports before 1.0
is a separate decision; `py.typed` makes every annotation a compatibility promise.
