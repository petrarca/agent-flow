"""Graceful happy-path shutdown of a finished agent (_stop_process).

On a normal completion (control sidecar written / terminal event) the supervisor
must give the subprocess a grace window to exit ON ITS OWN, and escalate to a
group kill ONLY if it lingers past the grace. These tests isolate that policy by
faking the process's self-exit timing and observing whether the (real) group-kill
would be invoked — the OS-signal mechanics of `_kill_group` are unchanged and not
re-tested here.
"""

import anyio
import pytest

import agent_flow.runners.supervision as sup


class _FakeProc:
    """A minimal anyio.Process stand-in: `wait()` returns after `exit_after` s."""

    def __init__(self, exit_after: float) -> None:
        self.pid = 4321
        self._exit_after = exit_after
        self.wait_calls = 0

    async def wait(self) -> int:
        self.wait_calls += 1
        await anyio.sleep(self._exit_after)
        return 0


@pytest.mark.anyio
async def test_stop_process_no_kill_when_exits_within_grace(monkeypatch):
    # Process self-exits well within the grace window -> _kill_group NOT called.
    killed = {"n": 0}

    async def _fake_kill(_proc):
        killed["n"] += 1

    monkeypatch.setattr(sup, "_kill_group", _fake_kill)
    monkeypatch.setattr(sup, "_KILL_GRACE_S", 5)

    proc = _FakeProc(exit_after=0.0)  # exits immediately (the happy path)
    await sup._stop_process(proc)

    assert proc.wait_calls == 1  # we waited for the clean exit
    assert killed["n"] == 0  # ... and did NOT kill it


@pytest.mark.anyio
async def test_stop_process_kills_when_it_lingers(monkeypatch):
    # Process overstays the grace window -> escalate to _kill_group.
    killed = {"n": 0}

    async def _fake_kill(_proc):
        killed["n"] += 1

    monkeypatch.setattr(sup, "_kill_group", _fake_kill)
    monkeypatch.setattr(sup, "_KILL_GRACE_S", 0.05)  # tiny grace for the test

    proc = _FakeProc(exit_after=10.0)  # never exits within grace
    await sup._stop_process(proc)

    assert killed["n"] == 1  # lingered past grace -> killed
