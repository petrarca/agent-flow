"""`coerce_schema` — the factory that turns an accepted schema form into a
`ResultSchema`.

It lives apart from the types it returns on purpose. The factory must know
every concrete implementation (`JsonSchema`, `PydanticSchema`) while each
implementation only needs the shared `ValidationOutcome` type — so putting the
factory beside the types forced `schema` to reach back into `schema_pydantic`
through a function-local import to avoid a cycle. Hoisting it one level up
turns that cycle into a DAG:

    coerce -> schema_pydantic -> schema
"""

from __future__ import annotations

from pydantic import BaseModel

from agent_flow.protocol.schema import JsonSchema, ResultSchema
from agent_flow.protocol.schema_pydantic import PydanticSchema


def coerce_schema(schema: object) -> ResultSchema | None:
    """Normalize the caller-supplied schema to a ResultSchema (or None).

    Accepts:
      - None -> no schema,
      - a raw JSON-schema dict -> wrapped in JsonSchema,
      - a pydantic BaseModel SUBCLASS -> wrapped in PydanticSchema (the common,
        obvious case — pydantic is a core dependency),
      - a ResultSchema implementation -> used as-is.

    Typed `object` on purpose: this is the boundary that VALIDATES an untyped,
    caller-supplied value (it reaches here as `Node.result_schema` /
    `AgentInvocation.result_schema`, both `object`), and anything unsupported
    raises TypeError below. A narrower annotation would just push a cast onto
    every call site without making the input any more trustworthy.
    """
    if schema is None:
        return None
    if isinstance(schema, dict):
        return JsonSchema(schema)
    if isinstance(schema, ResultSchema):
        return schema
    if isinstance(schema, type) and issubclass(schema, BaseModel):
        return PydanticSchema(schema)
    raise TypeError(f"result_schema must be a ResultSchema, a dict, a pydantic BaseModel subclass, or None — got {type(schema).__name__}")
