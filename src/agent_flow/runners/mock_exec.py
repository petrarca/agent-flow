"""MockExecutor — a deterministic stand-in for a real agent (the MockRuntime).

The out-of-process complement to opencode, run IN-PROCESS with no tokens. It is
selected by the `--mock-agents` substitution MODE (mock_agents=True), NOT by the
`runtime` axis — mock is not a runtime. When the mode is on, a node whose `agent`
has a registered `mock_agent` behaviour runs here instead of its normal executor.

Two layers, kept separate on purpose:

  - INNER behaviour: `mock_agent(inv, ctx) -> control_envelope`. Pure. Reads
    STRUCTURED inputs and may write files via `ctx` (MockAgentContext tools), and
    returns a control envelope (`{status, result?, rerun_required?}`) — the same
    shape a real agent writes to its sidecar. It knows nothing about sidecars.

  - OUTER surrounding (this executor, the MockRuntime): it invokes the behaviour,
    then does what a real out-of-process runner does — WRITES the control sidecar
    to disk (same path/shape opencode would), and assembles the AgentResult via
    the shared `AgentExecutor.assemble_result` tail (schema validation included).

`MockExecutor` is a SIBLING of SubprocessExecutor / InProcessExecutor at the
AgentExecutor ABC level — not a subclass of either. It shares only the
result-assembly tail (hoisted onto the ABC). See docs design/mock-agent.md.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from inspect import isawaitable
from pathlib import Path
from typing import Any

import anyio

from agent_flow.runners.executor import AgentExecutor, AgentResult
from agent_flow.runners.invocation import AgentInvocation
from agent_flow.utils import resolve_template

# A mock_agent behaviour: structured inputs + tools in, a control envelope out.
MockAgent = Callable[["AgentInvocation", "MockAgentContext"], Any]


class MockAgentContext:
    """The 'tools' a mock_agent has — the simulated counterpart of a real agent's
    toolset. Primitives only, zero policy.

    - `input(key, default)` reads a STRUCTURED work-order input (the resolved
      `inputs={...}` value the flow author wired).
    - `write_file(path, content)` / `read_file(path)` are the file tools; paths
      accept the same `{run_dir}`/`{param}` templating the flow's inputs use.

    Deliberately no `write_report`/domain helpers (that would bake policy), no
    `edit` (compose from read+write), no `exists`/`list` (unused). The `result`
    payload and `rerun_required` signal are NOT tools — they are fields of the
    returned control envelope.
    """

    def __init__(self, inv: AgentInvocation, work_order: dict[str, str], tmpl: dict[str, Any]) -> None:
        # inv is kept for callers that need access to the full invocation context
        # (e.g. run_context, instructions) for diagnostic/logging purposes.
        # It is deliberately NOT the primary input channel: structured inputs go
        # via work_order / self.input(); the composed prompt is for diagnostics.
        self.inv = inv
        self._work_order = dict(work_order)
        self._tmpl = dict(tmpl)

    def input(self, key: str, default: str | None = None) -> str | None:
        """Read a structured work-order input by key."""
        return self._work_order.get(key, default)

    def _resolve(self, path: str) -> Path:
        return Path(resolve_template(path, self._tmpl))

    def write_file(self, path: str, content: str) -> Path:
        """Write `content` to `path` ({run_dir}/{param} templated). Returns the path."""
        p = self._resolve(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
        return p

    def read_file(self, path: str) -> str:
        """Read `path` ({run_dir}/{param} templated) as text."""
        return self._resolve(path).read_text()


class MockExecutor(AgentExecutor):
    """Run a `mock_agent` behaviour and materialise the runner surrounding.

    Given the (already-resolved) behaviour, calls it with the invocation + a
    MockAgentContext, writes the returned control envelope to a sidecar on disk,
    and assembles the AgentResult via the shared ABC tail. The node bakes the
    behaviour on (like an in-process `impl`), so the executor needs no registry.
    """

    name = "mock"

    def __init__(self, behaviour: MockAgent, *, work_order: dict[str, str] | None = None, tmpl: dict[str, Any] | None = None) -> None:
        if not callable(behaviour):
            raise TypeError(f"MockExecutor: behaviour must be callable, got {type(behaviour).__name__!r}")
        self._behaviour = behaviour
        self._work_order = work_order or {}
        self._tmpl = tmpl or {}

    async def run(self, inv: AgentInvocation, *, control_file: Path | None = None) -> AgentResult:
        ctx = MockAgentContext(inv, self._work_order, self._tmpl)
        behaviour = self._behaviour

        # Mock behaviours are deterministic and fast, so call inline; an async
        # behaviour (rare, but allowed for symmetry with in-process impls) is
        # awaited. No thread offload — a mock must not block on real I/O.
        start = anyio.current_time()
        raw = behaviour(inv, ctx)
        if isawaitable(raw):
            raw = await raw
        duration = anyio.current_time() - start

        control = self._coerce_envelope(raw, inv.agent)

        # Materialise the sidecar on disk, like a real runner (the MockRuntime's
        # surrounding). Same default path SubprocessExecutor derives.
        run_dir = inv.run_dir
        run_dir.mkdir(parents=True, exist_ok=True)
        if control_file is None:
            base = inv.node or inv.agent
            control_file = run_dir / f"{base}.control.json"
        control_file.write_text(json.dumps(control))

        result = self.assemble_result(inv, control, exit_code=0, duration_s=duration, completion="completed", runtime=self.name)
        # Shared content-status policy: raise AgentContentFailedError for any
        # non-ok/non-verified status — identical to the subprocess path. Without
        # this, a mock returning {"status":"error"} would silently look like
        # success to the engine (no subprocess, no separate stale check).
        self.check_content_status(inv.agent, control)
        return result

    @staticmethod
    def _coerce_envelope(raw: Any, agent: str) -> dict:
        """Normalise a behaviour's return into a control envelope dict.

        None -> a bare ok. A dict is the envelope as-is (status defaulted to
        "ok"). Anything else is a mistake — a mock_agent must return the control
        envelope, not a bare payload (that ambiguity is what distinguishes it from
        an in-process agent_impl).
        """
        if raw is None:
            return {"status": "ok", "agent": agent}
        if isinstance(raw, dict):
            control = dict(raw)
            control.setdefault("status", "ok")
            control.setdefault("agent", agent)
            return control
        raise TypeError(f"mock_agent {agent!r} must return a control-envelope dict (or None), got {type(raw).__name__}")
