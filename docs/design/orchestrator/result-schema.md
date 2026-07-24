---
type: Concept
title: Result schema — typed agent output
description: The optional, Pydantic-agnostic seam for typing an agent's result payload; injection and non-failing validation.
tags: [agent-flow, result-schema, typed-output, pydantic, optional]
timestamp: 2026-07-23T07:51:35Z
---

# Result schema (typed output)

An agent's [control file](control-file.md) carries a free-form `result` object.
Typed output is an **opt-in consumer convenience** — the library imposes no
schema. When (and only when) a consumer attaches one to a run, the engine:

1. **injects** the schema into the prompt (so the agent knows the shape to emit),
   alongside the completion protocol, and
2. after the run, **validates** `control["result"]` and **attaches** the outcome
   (`result_valid`, `result_obj`, `result_errors`) to the `AgentResult`.

The engine **never fails** a run on a validation error. That is a flow-control
decision, so it is surfaced to the [gate](gates.md), which decides
Restart/Continue/Stop. Via the batteries layer a gate reads these as
`ctx.result["_result_valid"]` / `["_result_errors"]`, and the typed object as
`ctx.result["_result_obj"]` — a Pydantic instance for `PydanticSchema`, else
`None` (a dict schema / no schema add no new object; the validated data is the
dict already in `ctx.result["result"]`). (Validating here is
a convenience so the common case needs no gate code; a purist consumer may pass
no schema and validate inside their own gate instead. Either way, deciding what
to DO about a bad result stays the consumer's.)

## Runtime-agnostic, Pydantic-optional

The core depends only on a tiny protocol, never on Pydantic:

```python
class ResultSchema(Protocol):
    def to_json_schema(self) -> dict: ...
    def validate(self, data) -> ValidationOutcome: ...
```

Two ways to supply a schema:

- **A plain JSON-schema `dict`** — no third-party dependency. Wrapped in
  `JsonSchema`; validated with the `jsonschema` package if present, otherwise
  advisory (the schema was still injected into the prompt).
- **A `ResultSchema` implementation.** The optional `pydantic` extra ships
  `PydanticSchema`, which wraps a `BaseModel` and yields validated model
  **instances** in `result_obj`.

```python
# Pydantic (extra installed):
class TechStackResult(BaseModel):
    summary: str
    languages: list[str]
agent_node("tech-stack", "tech-stack-analyst", result_schema=PydanticSchema(TechStackResult))

# or a plain dict (no Pydantic):
agent_node("tech-stack", "tech-stack-analyst",
           result_schema={"type": "object", "required": ["summary", "languages"], ...})
```

## Why this is unlike Pydantic AI

Frameworks like Pydantic AI weld an output type to *their* in-process LLM call
(`agent.run_sync(output_type=…)`). Here the schema only touches the **prompt**
(injection) and the **control file** (validation), so it rides over ANY runner —
opencode subprocess, Claude Code CLI, mock — because the runtime abstraction
(`AgentRunner`) is preserved. Pydantic stays optional; a raw JSON-schema dict
works with zero heavy dependencies.

## Where it lives

`src/agent_flow/schema.py` (`ResultSchema`, `JsonSchema`, `ValidationOutcome`,
`coerce_schema`), `src/agent_flow/schema_pydantic.py` (`PydanticSchema`, behind
the `pydantic` extra), and the injection/validation in `agent_runtime.run_agent`.
