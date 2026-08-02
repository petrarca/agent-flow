"""Unit tests for gates.signals + the shipped gates that read them.

produced() (file check), rerun_targets()/read_field() (envelope), and the gates
rerun_on_signal / rerun_on_named / stop_if.
"""

from pathlib import Path

from agent_flow.gates import Continue, GateContext, GoTo, Stop, rerun_on_named, rerun_on_signal, stop_if
from agent_flow.gates.signals import produced, read_field, rerun_targets


class _N:
    """Minimal node stand-in (gates read node.name)."""

    def __init__(self, name):
        self.name = name


def _ctx(node_name, rerun=None, *, result=None, obj=None):
    """GateContext whose result is the HARVESTED control envelope (no file)."""
    if result is None:
        control: dict = {"status": "verified", "agent": "x"}
        if rerun is not None:
            control["rerun_required"] = rerun
    else:
        control = result
    return GateContext(result=control, node=_N(node_name), run_dir=Path("/tmp/run"), cycles=0, obj=obj, params={})


def test_rerun_on_signal_fixed_target_when_signalled():
    d = rerun_on_signal(_ctx("verify", ["analyst"]), target="analyst")
    assert isinstance(d, GoTo) and d.node == "analyst"


def test_rerun_on_signal_continue_when_no_signal():
    assert isinstance(rerun_on_signal(_ctx("verify"), target="analyst"), Continue)


def test_rerun_on_named_routes_to_named_node():
    # A coherence check names WHICH node to re-run; the gate GoTo's exactly that.
    d = rerun_on_named(_ctx("consistency", ["extractor"]))
    assert isinstance(d, GoTo) and d.node == "extractor"


def test_rerun_on_named_first_of_multiple():
    d = rerun_on_named(_ctx("consistency", ["summary", "analyst"]))
    assert isinstance(d, GoTo) and d.node == "summary"


def test_rerun_on_named_continue_when_no_signal():
    assert isinstance(rerun_on_named(_ctx("consistency")), Continue)


# --- rerun_targets: pure envelope extraction, no I/O ---------------------------


def test_rerun_targets_none_or_non_dict():
    assert rerun_targets(None) == []
    assert rerun_targets("not a dict") == []


def test_rerun_targets_no_field():
    assert rerun_targets({"status": "verified", "agent": "domain-verifier"}) == []


def test_rerun_targets_list():
    assert rerun_targets({"status": "verified", "rerun_required": ["domain-analyst"]}) == ["domain-analyst"]


def test_rerun_targets_string_coerced_to_list():
    assert rerun_targets({"rerun_required": "coupling-analyst"}) == ["coupling-analyst"]


def test_rerun_targets_empty_string_ignored():
    assert rerun_targets({"rerun_required": "  "}) == []


# --- produced: genuine filesystem check of the agent's work product -----------


def test_produced_true_for_nonempty(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("# hi")
    assert produced(p) is True


def test_produced_false_for_missing(tmp_path):
    assert produced(tmp_path / "nope.md") is False


def test_produced_false_for_empty(tmp_path):
    p = tmp_path / "r.md"
    p.write_text("")
    assert produced(p) is False


# --- read_field: typed-object-or-dict-agnostic field access --------------------


class _Obj:
    """A typed result stand-in (what ctx.obj is when a result_schema is set)."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


def test_read_field_prefers_typed_obj():
    # ctx.obj (typed) wins over a same-named key in the raw envelope.
    ctx = _ctx("readiness", obj=_Obj(ready="yes"), result={"ready": "no"})
    assert read_field(ctx, "ready") == "yes"


def test_read_field_falls_back_to_envelope_top_level():
    ctx = _ctx("readiness", result={"status": "ok", "ready": "no"})
    assert read_field(ctx, "ready") == "no"


def test_read_field_falls_back_to_result_payload():
    # An untyped pipeline usually puts structured data under the `result` payload.
    ctx = _ctx("readiness", result={"status": "ok", "result": {"ready": "partial"}})
    assert read_field(ctx, "ready") == "partial"


def test_read_field_default_when_absent():
    assert read_field(_ctx("readiness", result={"status": "ok"}), "ready") is None
    assert read_field(_ctx("readiness", result={"status": "ok"}), "ready", "x") == "x"


def test_read_field_none_valued_field_is_not_missing():
    # A field explicitly set to None is a value, not "absent" -> returns None, not default.
    assert read_field(_ctx("x", obj=_Obj(ready=None)), "ready", "DFLT") is None


# --- stop_if: STOP on a result field hitting a sentinel ------------------------


def test_stop_if_trips_on_match_with_label_and_reason():
    ctx = _ctx("readiness", obj=_Obj(ready="no", reason="product repo missing"))
    d = stop_if(ctx, field="ready", equals="no", label="tech-assessment")
    assert isinstance(d, Stop)
    assert d.reason == "tech-assessment: product repo missing"


def test_stop_if_continue_when_no_match():
    ctx = _ctx("readiness", obj=_Obj(ready="yes", reason=""))
    assert isinstance(stop_if(ctx, field="ready", equals="no"), Continue)


def test_stop_if_reads_dict_envelope_without_schema():
    # No typed obj: the gate reads the field from the raw envelope's result payload.
    ctx = _ctx("readiness", result={"status": "ok", "result": {"ready": "no", "reason": "nope"}})
    d = stop_if(ctx, field="ready", equals="no", label="sec")
    assert isinstance(d, Stop) and d.reason == "sec: nope"


def test_stop_if_reason_falls_back_to_field_summary():
    # No reason field present -> the Stop reason states which field tripped.
    ctx = _ctx("readiness", result={"ready": "no"})
    d = stop_if(ctx, field="ready", equals="no")
    assert isinstance(d, Stop) and d.reason == "ready == 'no'"


def test_stop_if_partial_does_not_trip_on_no():
    # Only the exact sentinel trips: 'partial' proceeds when the sentinel is 'no'.
    ctx = _ctx("readiness", obj=_Obj(ready="partial", reason="weaker"))
    assert isinstance(stop_if(ctx, field="ready", equals="no"), Continue)
