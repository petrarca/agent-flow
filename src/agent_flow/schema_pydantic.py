"""Pydantic adapter for the result-schema seam.

Wraps a `BaseModel` subclass as a `ResultSchema`, so the engine can inject the
model's JSON schema into the prompt and return validated model INSTANCES.

The core (`schema.py`, `agent_runtime.py`) does not import this — it depends only
on the `ResultSchema` protocol, so the schema library stays a detail of the
consumer's chosen adapter (this one, or a plain JSON-schema dict).
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ValidationError

from agent_flow.schema import ValidationOutcome


class PydanticSchema:
    """A ResultSchema backed by a Pydantic model.

    to_json_schema() -> the model's JSON schema (injected into the prompt).
    validate(data)   -> a validated model instance on success, or errors.
    """

    def __init__(self, model: type[BaseModel]) -> None:
        self._model = model

    def to_json_schema(self) -> dict:
        return self._model.model_json_schema()

    def validate(self, data: Any) -> ValidationOutcome:
        try:
            obj = self._model.model_validate(data)
        except ValidationError as exc:
            errors = tuple(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors())
            return ValidationOutcome(valid=False, errors=errors or (str(exc),))
        return ValidationOutcome(valid=True, obj=obj)
