"""Unit tests for the result-schema seam (JsonSchema + Pydantic adapter)."""

import pytest

from agent_flow.core.schema import JsonSchema, ResultSchema, ValidationOutcome, coerce_schema

_JSON_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}, "n": {"type": "integer"}},
    "required": ["summary", "n"],
}


def test_coerce_none():
    assert coerce_schema(None) is None


def test_coerce_dict_wraps_jsonschema():
    s = coerce_schema(_JSON_SCHEMA)
    assert isinstance(s, JsonSchema)
    assert s.to_json_schema() == _JSON_SCHEMA


def test_coerce_rejects_bad_type():
    with pytest.raises(TypeError):
        coerce_schema(42)


def test_coerce_pydantic_model_class_wraps_pydanticschema():
    from pydantic import BaseModel

    from agent_flow.core.schema_pydantic import PydanticSchema

    class R(BaseModel):
        x: int

    s = coerce_schema(R)
    assert isinstance(s, PydanticSchema)
    # produces the model's JSON schema for prompt injection
    assert "properties" in s.to_json_schema()


def test_jsonschema_valid():
    out = JsonSchema(_JSON_SCHEMA).validate({"summary": "x", "n": 3})
    assert out.valid is True
    assert out.errors == ()
    # A dict schema produces NO separate typed object (the dict is already the
    # result); only a Pydantic model yields obj. So obj is None here.
    assert out.obj is None


def test_jsonschema_invalid():
    out = JsonSchema(_JSON_SCHEMA).validate({"summary": "x"})  # missing n
    assert out.valid is False
    assert out.errors


def test_jsonschema_is_a_resultschema():
    assert isinstance(JsonSchema(_JSON_SCHEMA), ResultSchema)


def test_validation_outcome_defaults():
    o = ValidationOutcome(valid=True)
    assert o.obj is None
    assert o.errors == ()


# Pydantic adapter (pydantic is a core dependency).


def test_pydantic_schema_valid_returns_instance():
    from pydantic import BaseModel

    from agent_flow.core.schema_pydantic import PydanticSchema

    class R(BaseModel):
        summary: str
        n: int

    s = PydanticSchema(R)
    assert "properties" in s.to_json_schema()
    out = s.validate({"summary": "x", "n": 3})
    assert out.valid is True
    assert isinstance(out.obj, R)
    assert out.obj.n == 3


def test_pydantic_schema_invalid_reports_errors():
    from pydantic import BaseModel

    from agent_flow.core.schema_pydantic import PydanticSchema

    class R(BaseModel):
        summary: str
        n: int

    out = PydanticSchema(R).validate({"summary": "x"})  # missing n
    assert out.valid is False
    assert any("n" in e for e in out.errors)
