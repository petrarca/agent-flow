"""Unit test: the supervision layer forwards real events to on_event."""

from agent_flow.runners import Event
from agent_flow.runners.subprocess_exec import _apply_event


class _FakeRunner:
    """Parses a line into a preset event (or none for empty lines)."""

    name = "fake"

    def parse_event(self, line: str) -> Event:
        line = line.strip()
        if not line:
            return Event.none()
        return Event(raw=line)

    def build_command(self, inv):  # pragma: no cover - unused here
        from agent_flow.runners.base import LaunchSpec

        return LaunchSpec(argv=[], display="")


def test_apply_event_calls_on_event_for_real_events():
    seen = []
    st = {"tokens": 0, "cost": 0.0, "events": 0, "saw_terminal": False}
    is_ev = _apply_event("read /x", st, _FakeRunner(), seen.append)
    assert is_ev is True
    assert len(seen) == 1
    assert seen[0].raw == "read /x"


def test_apply_event_skips_non_events():
    seen = []
    st = {"tokens": 0, "cost": 0.0, "events": 0, "saw_terminal": False}
    is_ev = _apply_event("   ", st, _FakeRunner(), seen.append)
    assert is_ev is False
    assert seen == []


def test_on_event_error_never_propagates():
    def boom(_ev):
        raise RuntimeError("display broke")

    st = {"tokens": 0, "cost": 0.0, "events": 0, "saw_terminal": False}
    # Must not raise despite the callback throwing.
    assert _apply_event("x", st, _FakeRunner(), boom) is True
