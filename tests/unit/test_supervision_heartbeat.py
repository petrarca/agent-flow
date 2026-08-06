"""The heartbeat: a silent agent must still show that the run is alive.

A node with a long idle budget can legitimately sit quiet for minutes. Without a
beat that is indistinguishable from a hung ORCHESTRATOR — no output at all, and
nothing to say which of several parallel agents is holding the run up. So while
an agent is quiet, supervision reports periodically: who it is waiting on, how
long it has been silent, and how much budget is left.

An agent that is EMITTING needs no beat: its events already prove liveness, and
a beat there would be noise on every healthy run.
"""

from __future__ import annotations

import anyio
import pytest

import agent_flow.runners.supervision as sup
from agent_flow.runners.events import Event


class _FakeProc:
    pid = 999

    async def wait(self) -> int:
        return 0


class _Runner:
    """Minimal AgentRunner: any non-blank line is a real event."""

    name = "fake"

    def parse_event(self, line: str) -> Event | None:
        return Event(raw={}, is_event=True) if line.strip() else None


async def _noop(*_args, **_kwargs) -> None:
    return None


async def _drive(monkeypatch, *, lines, idle_timeout_s, heartbeat_s):
    """Run the loop over `lines`, capturing the heartbeat lines it logs."""
    beats: list[str] = []
    monkeypatch.setattr(sup, "_HEARTBEAT_S", heartbeat_s)
    monkeypatch.setattr(sup, "_POLL_CAP_S", 0.02)
    monkeypatch.setattr(sup.logger, "info", lambda msg: beats.append(msg))
    monkeypatch.setattr(sup, "_kill_group", _noop)
    monkeypatch.setattr(sup, "_stop_process", _noop)
    monkeypatch.setattr(sup, "_sidecar_probe", lambda _cf: lambda: False)

    tx, rx = anyio.create_memory_object_stream(50)
    for line in lines:
        await tx.send(line)

    await sup._supervise_loop(
        _FakeProc(),
        runner=_Runner(),
        idle_timeout_s=idle_timeout_s,
        control_file=None,
        on_event=None,
        stdout_rx=rx,
        stderr_rx=None,
        agent="domain-verifier",
    )
    return beats


@pytest.mark.anyio
async def test_a_silent_agent_is_reported_with_its_name_and_budget(monkeypatch):
    # Total silence until stale: the operator must learn WHO is being waited on,
    # how long it has been quiet, and how much budget remains.
    beats = await _drive(monkeypatch, lines=[], idle_timeout_s=0.3, heartbeat_s=0.05)

    assert beats, "a silent agent produced no heartbeat"
    assert "domain-verifier" in beats[0]
    assert "silent for" in beats[0]
    assert "left" in beats[0]


@pytest.mark.anyio
async def test_the_beat_repeats_while_the_silence_lasts(monkeypatch):
    # One beat is not enough — the point is a periodic sign of life.
    beats = await _drive(monkeypatch, lines=[], idle_timeout_s=0.4, heartbeat_s=0.05)

    assert len(beats) > 1, f"expected repeated beats, got {beats}"


@pytest.mark.anyio
async def test_an_emitting_agent_is_never_beaten_about(monkeypatch):
    # Its events ARE the heartbeat. Beating here would put a line on every
    # healthy run, which is exactly the noise that makes operators stop reading.
    # The agent must be emitting OVER TIME: a burst that lands at once leaves the
    # agent genuinely quiet afterwards, and a beat then is correct.
    beats: list[str] = []
    monkeypatch.setattr(sup, "_HEARTBEAT_S", 0.5)
    monkeypatch.setattr(sup, "_POLL_CAP_S", 0.02)
    monkeypatch.setattr(sup.logger, "info", lambda msg: beats.append(msg))
    monkeypatch.setattr(sup, "_kill_group", _noop)
    monkeypatch.setattr(sup, "_stop_process", _noop)
    monkeypatch.setattr(sup, "_sidecar_probe", lambda _cf: lambda: False)

    tx, rx = anyio.create_memory_object_stream(50)

    async def talk() -> None:
        for i in range(20):  # ~1s of steady chatter, well inside the beat window
            await tx.send(f"event {i}\n")
            await anyio.sleep(0.05)
        await tx.send(None)  # EOF: the process finished normally

    async with anyio.create_task_group() as tg:
        tg.start_soon(talk)
        await sup._supervise_loop(
            _FakeProc(),
            runner=_Runner(),
            idle_timeout_s=5.0,
            control_file=None,
            on_event=None,
            stdout_rx=rx,
            stderr_rx=None,
            agent="domain-verifier",
        )

    assert beats == [], f"an active agent should stay silent in the log, got {beats}"
