"""FlowDef.params_schema — the flow declares its own SIGNATURE.

A node declares its input contract (`input_schema`) and its output contract
(`result_schema`), both registered BY NAME. A FLOW declares the run parameters it
needs the same way: `FlowDef(params_schema="CloudParams")` naming a model
registered via `registry.params_model(...)`.

Why it matters: the pairing "flow <-> its params" now travels WITH the flow. Two
flows in one app can no longer be started with each other's params model, and a
serialized FlowDef is self-describing about what it needs to run.
"""

import anyio
import pytest
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings

from agent_flow.flowdef import FlowDef, NodeDef, compile_flow
from agent_flow.registry import FlowRegistry


def _registry_with(model, name="P"):
    """A registry with the params model registered + a mock agent that records
    the RESOLVED inputs (so we can assert templating saw the validated params)."""
    reg = FlowRegistry()
    reg.params_model(name)(model)
    seen: dict = {}

    @reg.mock_agent("a")
    def _cap(inv, ctx):  # noqa: ARG001
        seen.update(inv.inputs)
        return {"status": "ok"}

    return reg, seen


class Params(BaseModel):
    """A PLAIN BaseModel is enough — values come from -p / kwargs."""

    product_key: str = Field(description="required")
    depth: int = 3


class SettingsParams(BaseSettings):
    """A BaseSettings adds bare-env/.env fallback; both are accepted."""

    model_config = {"env_file": ".env", "extra": "ignore"}
    product_key: str = "from-default"


# --- the registry seam ------------------------------------------------------


def test_params_model_registers_and_resolves():
    reg = FlowRegistry()
    reg.params_model("P")(Params)
    assert reg.has_params_model("P")
    assert reg.get_params_model("P") is Params


def test_params_model_decorator_form():
    reg = FlowRegistry()

    @reg.params_model("P")
    class Local(BaseModel):
        x: int = 1

    assert reg.get_params_model("P") is Local


def test_unknown_params_model_raises_with_the_registered_names():
    reg = FlowRegistry()
    reg.params_model("Known")(Params)
    with pytest.raises(ValueError, match="unknown params_schema"):
        reg.get_params_model("Nope")


def test_params_namespace_is_separate_from_node_schemas():
    """A flow's params contract and a node's result schema are different concepts
    and must not collide on a name."""
    reg = FlowRegistry()
    reg.params_model("Same")(Params)
    reg.schema("Same")(SettingsParams)
    assert reg.get_params_model("Same") is Params  # not the node schema
    assert reg.get_schema("Same") is SettingsParams


# --- compile-time validation ------------------------------------------------


def test_compile_rejects_an_unknown_params_schema():
    reg, _ = _registry_with(Params)
    flow = FlowDef(name="t", params_schema="Missing", nodes=[NodeDef(name="n", agent="a")])
    with pytest.raises(ValueError, match="unknown params_schema"):
        compile_flow(flow, reg)


def test_compile_accepts_a_registered_params_schema():
    reg, _ = _registry_with(Params)
    flow = FlowDef(name="t", params_schema="P", nodes=[NodeDef(name="n", agent="a")])
    assert len(compile_flow(flow, reg)) == 1


def test_params_schema_is_optional():
    reg, _ = _registry_with(Params)
    flow = FlowDef(name="t", nodes=[NodeDef(name="n", agent="a")])
    assert flow.params_schema is None
    assert len(compile_flow(flow, reg)) == 1


def test_flowdef_stays_serializable_with_params_schema():
    """The NAME travels; the model class stays in code — so a FlowDef with a
    params contract still round-trips as data."""
    flow = FlowDef(name="t", params_schema="P", nodes=[NodeDef(name="n", agent="a")])
    assert '"params_schema": "P"' in flow.to_json()


# --- the flow's signature is APPLIED (both entry points) --------------------


def test_run_flow_validates_against_the_declared_schema(tmp_path):
    """A missing required param must fail via the flow's OWN declaration — no
    params_model= passed anywhere."""
    from pydantic import ValidationError

    from agent_flow import run_flow

    reg, _ = _registry_with(Params)
    flow = FlowDef(name="t", params_schema="P", nodes=[NodeDef(name="n", agent="a")])
    with pytest.raises(ValidationError):
        run_flow(flow, registry=reg, run_dir=str(tmp_path), mock_agents=True)  # product_key missing


def test_run_flow_applies_defaults_from_the_declared_schema(tmp_path):
    from agent_flow import run_flow

    reg, seen = _registry_with(Params)
    flow = FlowDef(
        name="t",
        params_schema="P",
        nodes=[NodeDef(name="n", agent="a", inputs={"K": "{product_key}", "D": "{depth}"})],
    )
    run_flow(flow, registry=reg, run_dir=str(tmp_path), mock_agents=True, product_key="acme")
    assert seen == {"K": "acme", "D": "3"}  # default depth=3 applied + templated


def test_arun_flow_honours_the_declared_schema(tmp_path):
    from agent_flow import arun_flow

    reg, seen = _registry_with(Params)
    flow = FlowDef(name="t", params_schema="P", nodes=[NodeDef(name="n", agent="a", inputs={"K": "{product_key}"})])
    anyio.run(lambda: arun_flow(flow, registry=reg, run_dir=str(tmp_path), mock_agents=True, product_key="globex"))
    assert seen == {"K": "globex"}


def test_run_cli_resolves_params_model_from_the_flow():
    """run_cli picks the model off the flow — the call site no longer has to
    remember which model pairs with which flow."""
    import inspect

    from agent_flow.cli import app as app_mod

    src = inspect.getsource(app_mod.run_cli)
    assert "flow_def.params_schema" in src
    assert "get_params_model" in src


def test_explicit_params_model_overrides_the_declared_one():
    """The imperative escape hatch still wins (and covers the build_nodes form,
    which has no FlowDef to declare on)."""
    import inspect

    from agent_flow.cli import app as app_mod

    src = inspect.getsource(app_mod.run_cli)
    assert "if params_model is None and flow_def.params_schema:" in src


def test_validation_does_not_eat_the_framework_params(tmp_path):
    """REGRESSION: the programmatic bag carries framework keys (mock_agents,
    runtime, …) alongside domain params. A domain model IGNORES unknown fields,
    so validating the whole bag and taking the result would silently drop them —
    the run would then try to spawn a real runtime instead of mocking. Validated
    domain values must OVERLAY, not replace."""
    from agent_flow import run_flow

    reg, seen = _registry_with(Params)
    flow = FlowDef(name="t", params_schema="P", nodes=[NodeDef(name="n", agent="a", inputs={"K": "{product_key}"})])
    # mock_agents is NOT a field of Params; it must survive validation.
    run_flow(flow, registry=reg, run_dir=str(tmp_path), mock_agents=True, product_key="acme")
    assert seen == {"K": "acme"}, "mock_agents was dropped by params validation"


def test_a_settings_model_works_too(tmp_path):
    """BaseModel or BaseSettings — the library only constructs and dumps it."""
    from agent_flow import run_flow

    reg, seen = _registry_with(SettingsParams)
    flow = FlowDef(name="t", params_schema="P", nodes=[NodeDef(name="n", agent="a", inputs={"K": "{product_key}"})])
    run_flow(flow, registry=reg, run_dir=str(tmp_path), mock_agents=True, product_key="acme")
    assert seen == {"K": "acme"}
