---
type: Concept
title: Result schema — typed agent output
description: The optional, Pydantic-agnostic seam for typing an agent's result payload; injection and non-failing validation.
tags: [agent-flow, result-schema, typed-output, pydantic, optional]
timestamp: 2026-07-23T07:51:35Z
---

# Result schema (typed output)

An agent's [control file](control-file.md) carries a free-form `result` object.
Typed output is an opt-in consumer convenience — the library imposes no
schema. When (and only when) a consumer attaches one to a run, the engine:

1. **injects** the schema into the prompt (so the agent knows the shape to emit),
   alongside the completion protocol, and
2. after the run, validates `control["result"]` and attaches the outcome
   (`result_valid`, `result_obj`, `result_errors`) to the `AgentResult`.

The engine never fails a run on a validation error. That is a flow-control
decision, so it is surfaced to the [gate](gates.md), which decides
Restart/Continue/Stop. The gate reads the typed object as `ctx.obj` — a
Pydantic instance for `PydanticSchema`, else `None` (a dict schema / no schema add
no new object; the validated data is the dict at `ctx.result["result"]`). The
schema-check flags are on the envelope: `ctx.result["_result_valid"]` /
`["_result_errors"]`. (Validating here is
a convenience so the common case needs no gate code; a purist consumer may pass
no schema and validate inside their own gate instead. Either way, deciding what
to DO about a bad result stays the consumer's.)

## Runtime-agnostic, schema-library-agnostic

The engine depends only on a tiny protocol, not on any one schema library:

```python
class ResultSchema(Protocol):
    def to_json_schema(self) -> dict: ...
    def validate(self, data) -> ValidationOutcome: ...
```

Two ways to supply a schema:

- **A plain JSON-schema `dict`** — no schema class needed. Wrapped in
  `JsonSchema`; validated with the `jsonschema` package.
- **A `ResultSchema` implementation.** `PydanticSchema` wraps a `BaseModel` and
  yields validated model instances in `result_obj`.

```python
# Pydantic model:
class TechStackResult(BaseModel):
    summary: str
    languages: list[str]
agent_node("tech-stack", "tech-stack-analyst", result_schema=PydanticSchema(TechStackResult))

# or a plain dict:
agent_node("tech-stack", "tech-stack-analyst",
           result_schema={"type": "object", "required": ["summary", "languages"], ...})
```

## Why this is unlike Pydantic AI

Frameworks like Pydantic AI weld an output type to *their* in-process LLM call
(`agent.run_sync(output_type=…)`). Here the schema only touches the prompt
(injection) and the control file (validation), so it rides over ANY execution
model — an opencode/Claude Code subprocess, an in-process agent, or a
`--mock-agents` stand-in — because validation is the shared executor tail, not
tied to a runner. The core depends only on the `ResultSchema` protocol, not on
any one schema library, so a raw JSON-schema dict is a first-class alternative to
a Pydantic model.

## The mirror: `input_schema` (typed INPUTS)

The same concept applied on the way IN. A node's `inputs` carry the VALUES
(templated per node, serializable); `input_schema` carries their TYPE (shared,
referenced by registered name). Both use the SAME machinery — `coerce_schema` +
`ResultSchema.validate` — so a pydantic model or a plain JSON-schema dict works
in either position.

| | values | type |
|---|---|---|
| in | `inputs={...}` | `input_schema=` |
| out | the agent's `result` payload | `result_schema=` |

Two properties make it safe to add to an existing node:

- **It validates, it does not re-render.** The work order still renders with the
  keys the author wrote, so an agent `.md` that refers to `TICKET`/`REPORT` is
  unaffected. snake_case fields keep UPPERCASE wire keys via ordinary pydantic
  aliases — no case rule in the engine.
- **It runs on the RESOLVED work order**, after `{param}` templating and upstream
  `exports`, and BEFORE the agent is spawned. An unresolved `{mode}` therefore
  becomes a schema error rather than literal text handed to an agent — the
  failure mode this exists to remove. The catch is only as strong as the field:
  a bare `str` accepts `"{mode}"`, so a `Literal`/`pattern`/non-`str` type is
  what makes an unresolved placeholder detectable.

Unlike a bad *result* (which never auto-fails — a [gate](gates.md) decides,
because the agent has already run and its output is evidence), a bad *input* has
no result to inspect and nothing downstream can repair it, so it raises. That
raise is an ordinary node error: `interpret` maps it through the node's
`criticality` (blocking halts, degrade degrades).

An in-process impl receives the validated instance as `inv.input_obj`, alongside
the raw `inv.inputs`/`inv.params` — which is what lets an in-process agent be
typed at both ends while the same node definition still runs on a subprocess
runtime, which can only be handed text.

## Where it lives

`src/agent_flow/protocol/schema.py` (`ResultSchema`, `JsonSchema`, `ValidationOutcome`,
`coerce_schema`) and `src/agent_flow/protocol/schema_pydantic.py` (`PydanticSchema`).
**Injection** into the prompt is subprocess-specific (`SubprocessExecutor` embeds
the schema in the control preamble, `runners/subprocess_exec.py`). Validation is
the shared `AgentExecutor.assemble_result` in `src/agent_flow/runners/executor.py`
— used by `SubprocessExecutor`, `MockExecutor`, and (via `adapt_result`)
`InProcessExecutor` — so typed output behaves identically across execution
models, and `result_obj`/`result_valid`/`result_errors` are populated only by
that validation. All of these types are re-exported at the top level, so
consumers import them as `from agent_flow import PydanticSchema` (etc.); the
`protocol.schema_pydantic` path is
the internal location.

`input_schema` is validated by `node_builder._validate_inputs` (before the
executor is chosen) and surfaced on the invocation as `input_obj`.
