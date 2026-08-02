"""The FINISHING window: the sidecar is a verdict, not a stop signal.

An agent's control sidecar appears the moment its `write` tool runs — seconds
before its turn actually ends (it still has to return the tool result, close the
step, flush telemetry and shut its MCP children down). Treating the sidecar as
completion SIGTERM'd every agent mid-turn.

So supervision keeps consuming events after the sidecar lands and ends on the
real signal — the terminal event or EOF — bounded by `_FINISH_GRACE_S` for an
agent that writes its verdict and then never closes the turn.
"""

from __future__ import annotations

import anyio
import pytest

import agent_flow.runners.supervision as sup
from agent_flow.runners.events import Event


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 999
        self.waits = 0

    async def wait(self) -> int:
        self.waits += 1
        return 0


class _Runner:
    """Minimal AgentRunner: a line is an event; "DONE" is the terminal one."""

    name = "fake"

    def parse_event(self, line: str) -> Event | None:
        text = line.strip()
        if not text:
            return None
        return Event(raw={}, is_event=True, is_terminal=(text == "DONE"), tokens=0, cost=0.0)


async def _drive(lines, *, sidecar_after: int | None, idle_timeout_s=5.0, finish_grace=0.3, close=True):
    """Run the supervision loop over `lines`; the sidecar 'appears' after N lines."""
    proc = _FakeProc()
    sent, killed = [], []

    async def _fake_stop(p):
        sent.append(p)

    async def _fake_kill(p):
        killed.append(p)

    seen = {"n": 0}
    state = {"present": False}

    tx, rx = anyio.create_memory_object_stream(100)
    for ln in lines:
        await tx.send(ln)
    if close:
        await tx.send(None)  # EOF sentinel

    def probe():
        return state["present"]

    # flip the sidecar on after `sidecar_after` lines have been consumed
    real_consume = sup._consume_line

    def counting_consume(line, st, runner, on_event):
        out = real_consume(line, st, runner, on_event)
        seen["n"] += 1
        if sidecar_after is not None and seen["n"] >= sidecar_after:
            state["present"] = True
        return out

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(sup, "_stop_process", _fake_stop)
        mp.setattr(sup, "_kill_group", _fake_kill)
        mp.setattr(sup, "_sidecar_probe", lambda _cf: probe)
        mp.setattr(sup, "_consume_line", counting_consume)
        mp.setattr(sup, "_FINISH_GRACE_S", finish_grace)
        result = await sup._supervise_loop(
            proc,
            runner=_Runner(),
            idle_timeout_s=idle_timeout_s,
            control_file=None,
            on_event=None,
            stdout_rx=rx,
            stderr_rx=None,
        )
    return result, sent, killed


@pytest.mark.anyio
async def test_sidecar_alone_does_not_stop_the_agent():
    # The sidecar lands on line 1, but the agent keeps working for 3 more events
    # and only then says DONE. All 4 later events must still be consumed.
    result, sent, killed = await _drive(["a", "b", "c", "DONE"], sidecar_after=1)
    assert result.completion == "sidecar"
    assert result.events == 4  # nothing was cut off mid-turn
    assert killed == []  # never escalated to a group kill


@pytest.mark.anyio
async def test_terminal_event_after_sidecar_ends_it_cleanly():
    result, sent, killed = await _drive(["work", "DONE"], sidecar_after=1)
    assert result.completion == "sidecar"
    assert len(sent) == 1  # stopped via the graceful path
    assert killed == []


@pytest.mark.anyio
async def test_finish_window_bounds_an_agent_that_never_closes_its_turn():
    # Sidecar written, then silence: the finishing deadline must stop it.
    result, sent, killed = await _drive(["work"], sidecar_after=1, finish_grace=0.15, close=False)
    assert result.completion == "sidecar"
    assert len(sent) == 1  # stopped after the window, not killed outright
    assert killed == []


@pytest.mark.anyio
async def test_eof_after_sidecar_is_a_clean_completion():
    # The process ends its own stream -> reaped, no kill.
    result, sent, killed = await _drive(["work"], sidecar_after=1, finish_grace=30.0)
    assert result.completion in ("completed", "sidecar")
    assert killed == []


@pytest.mark.anyio
async def test_stale_without_a_sidecar_still_kills():
    # No sidecar and no output for the idle window -> genuinely hung.
    result, sent, killed = await _drive([], sidecar_after=None, idle_timeout_s=0.15, close=False)
    assert result.completion == "stale"
    assert len(killed) == 1  # killed immediately, no grace
    assert sent == []


@pytest.mark.anyio
async def test_idle_timeout_does_not_fire_while_finishing():
    # Once the verdict is on disk the idle deadline no longer applies — the
    # finishing window governs. A short idle timeout must NOT mark it stale.
    result, _sent, killed = await _drive(["work", "DONE"], sidecar_after=1, idle_timeout_s=0.05, finish_grace=5.0)
    assert result.completion == "sidecar"  # not "stale"
    assert killed == []
