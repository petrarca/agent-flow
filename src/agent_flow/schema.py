"""Result-schema seam — typed agent output without a hard Pydantic dependency.

An agent's control file carries a free-form `result` object. Typed output is an
OPT-IN CONSUMER CONVENIENCE — the library imposes no schema. When (and only when)
a consumer attaches a result schema to a run, the engine then:

  1. injects the schema into the prompt (so the agent knows the shape to emit),
  2. after the run, validates `control["result"]` against the schema and
     attaches the outcome (valid?, errors, parsed object) to the AgentResult.

The engine NEVER fails a run on a validation error — that is a flow-control
decision, so it is surfaced to the gate, which decides Restart/Continue/Stop.
(Validating here is a convenience so the common case needs no gate code; a
purist consumer may pass no schema and validate inside their own gate instead.
Either way, deciding what to DO about a bad result stays the consumer's.)

Runtime-agnostic by design: the core depends only on the tiny `ResultSchema`
protocol below, NOT on Pydantic. Two ways to supply a schema:

  - a plain JSON-schema `dict` (no third-party dependency) — injected into the
    prompt; validated with `jsonschema` if that package is available, otherwise
    treated as advisory (valid=True, no object).
  - a `ResultSchema` implementation. The optional `pydantic` extra ships
    `PydanticSchema`, which wraps a `BaseModel` and yields validated instances.

This is deliberately unlike frameworks that weld an output schema to one
in-process LLM call: here the schema rides over ANY runner (opencode subprocess,
Claude Code CLI, mock), because it only touches the prompt and the control file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class ResultSchema(Protocol):
    """Minimal contract the engine needs to type an agent's `result` payload."""

    def to_json_schema(self) -> dict:
        """Return a JSON-schema dict to inject into the agent prompt."""
        ...

    def validate(self, data: Any) -> ValidationOutcome:
        """Validate `data` (the control `result`) against the schema."""
        ...


@dataclass(frozen=True)
class ValidationOutcome:
    """Result of validating an agent's `result` payload against a schema.

    valid   True if `data` conforms (or no validation was possible/required).
    obj     a distinct TYPED object produced by validation (a Pydantic model
            instance), or None. It is None for a plain-dict JSON schema: the
            validated data is already the `result` dict, so there is nothing new
            to hand back. Only a Pydantic model yields a separate typed object.
    errors  human-readable error strings when invalid, else empty.
    """

    valid: bool
    obj: Any = None
    errors: tuple[str, ...] = ()


class JsonSchema:
    """A ResultSchema backed by a plain JSON-schema dict — no Pydantic.

    Validation uses the `jsonschema` package if importable; otherwise it is
    advisory (valid=True) since the schema was still injected into the prompt.
    `obj` is always None — a dict schema produces no NEW object (the validated
    data is the `result` dict itself). Only PydanticSchema yields a typed obj.
    """

    def __init__(self, schema: dict) -> None:
        self._schema = schema

    def to_json_schema(self) -> dict:
        return self._schema

    def validate(self, data: Any) -> ValidationOutcome:
        try:
            import jsonschema
        except ImportError:
            # Schema was injected into the prompt; without a validator we cannot
            # check conformance, so treat as advisory rather than failing.
            return ValidationOutcome(valid=True)
        try:
            jsonschema.validate(instance=data, schema=self._schema)
        except jsonschema.ValidationError as exc:
            return ValidationOutcome(valid=False, errors=(str(exc.message),))
        return ValidationOutcome(valid=True)


def coerce_schema(schema: ResultSchema | dict | None) -> ResultSchema | None:
    """Normalize the caller-supplied schema to a ResultSchema (or None).

    Accepts a ResultSchema implementation, a raw JSON-schema dict (wrapped in
    JsonSchema), or None (no schema).
    """
    if schema is None:
        return None
    if isinstance(schema, dict):
        return JsonSchema(schema)
    if isinstance(schema, ResultSchema):
        return schema
    raise TypeError(f"result_schema must be a ResultSchema, a dict, or None — got {type(schema).__name__}")
