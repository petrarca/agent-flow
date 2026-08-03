"""The library <-> agent AGREEMENT: how an agent is told to report, and what
shape its answer must take.

This is a pure LEAF package. It imports only the standard library, `jsonschema`
and `pydantic` — never `core`, `runners`, `engine`, `backends` or `cli`. Every
layer above may depend on it; it depends on none of them.

The two halves are one contract, not two neighbours. `build_control_preamble`
writes the control-sidecar protocol into the agent's prompt at run time, and the
result schema is EMBEDDED in that same preamble (`schema_dict`) — so a schema is
not merely adjacent to the protocol, it is transmitted as part of it.

Why it is its own package: both `core` (which supervises a run) and `runners`
(which build the argv and assemble a result) need these definitions. Filed under
either one, the other has to import upward. Below both, the dependency is
one-directional:

    core -> runners -> protocol

Module map:
  schema.py           the `ResultSchema` protocol, `ValidationOutcome`, `JsonSchema`
  schema_pydantic.py  `PydanticSchema` — the pydantic implementation
  coerce.py           `coerce_schema` — the factory over both implementations
  control.py          `build_control_preamble` — the control-sidecar protocol
  rerun.py            the re-run REQUEST: what a node grants (`RerunSpec`), what
                      an agent may write, how it parses (`parse_rerun`)
"""

from __future__ import annotations

from agent_flow.protocol.coerce import coerce_schema
from agent_flow.protocol.control import build_control_preamble
from agent_flow.protocol.rerun import RerunRequest, RerunSpec, parse_rerun
from agent_flow.protocol.schema import JsonSchema, ResultSchema, ValidationOutcome
from agent_flow.protocol.schema_pydantic import PydanticSchema

__all__ = [
    "JsonSchema",
    "PydanticSchema",
    "RerunRequest",
    "RerunSpec",
    "ResultSchema",
    "ValidationOutcome",
    "build_control_preamble",
    "coerce_schema",
    "parse_rerun",
]
