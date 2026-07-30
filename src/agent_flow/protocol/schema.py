"""Result-schema seam — typed agent output.

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

The engine depends only on the tiny `ResultSchema` protocol below, not on any
one schema library. Two ways to supply a schema:

  - a plain JSON-schema `dict` — injected into the prompt and validated with
    `jsonschema`.
  - a `ResultSchema` implementation. `schema_pydantic.PydanticSchema` wraps a
    `BaseModel` and yields validated instances.

This is deliberately unlike frameworks that weld an output schema to one
in-process LLM call: here validation is the shared executor tail
(`AgentExecutor.assemble_result`), so it rides over ANY execution model — an
opencode/Claude Code subprocess, an in-process agent, or a `--mock-agents`
stand-in — touching only the prompt and the control file.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

import jsonschema


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

    Validation uses the `jsonschema` package (a core dependency). `obj` is always
    None — a dict schema produces no NEW object (the validated data is the
    `result` dict itself). Only PydanticSchema yields a typed obj.
    """

    def __init__(self, schema: dict) -> None:
        self._schema = schema

    def to_json_schema(self) -> dict:
        return self._schema

    def validate(self, data: Any) -> ValidationOutcome:
        # Collect ALL errors (not just the first) for parity with PydanticSchema,
        # so a gate reading `result_errors` sees the full picture either way.
        validator = jsonschema.Draft202012Validator(self._schema)
        errors = tuple(e.message for e in validator.iter_errors(data))
        return ValidationOutcome(valid=not errors, errors=errors)
