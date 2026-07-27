"""Run-context service — the run-scoped store for OPEN, domain params.

Two different globals, deliberately separate (do not conflate them):

  - `RunConfig` (run_config.py, via `get_settings()`) — the LIBRARY's own
    settings: runtime, model, run_dir, agent_dir, idle_timeout. TYPED
    pydantic-settings with fixed fields, set once at startup.

  - THIS service (via `get_run_context()`) — the DOMAIN params: an OPEN mapping
    the library attaches no meaning to (product_key, product_repos_root,
    analysis_timestamp, a readiness-derived `mode`, …). These template `{name}`
    placeholders in node inputs/context/paths.

Why a service and not a dict threaded by reference: the engine used to pass a
plain `params` dict down the call chain (walk -> group -> task -> RunContext).
That is fragile — it only works because sequential nodes share the object, it
can't be updated mid-run cleanly, and a node that wants to PUBLISH a value for
downstream nodes has to reach into a passed-around dict. This service makes the
domain params a first-class, process-wide, run-scoped store you can `get_...`
from anywhere (nodes, gates, exports hooks), with a lock for the concurrent case.

House pattern (as in run_config / coco-rag): an lru_cache singleton via
`get_run_context()`, installed by `init_run_context(...)`, reset in tests by
`clear_run_context()`.

SCOPE / BOUNDARY (important, honest constraint):
  This is a SAME-PROCESS, run-scoped store. The engine runs nodes as concurrent
  anyio TASKS in one process (the in-process backend; the Prefect backend adds
  its own task runner), so a process-global singleton is the correct sharing
  mechanism and `update()` is visible to nodes that run LATER (downstream
  groups). It is NOT a distributed store: if the flow backend is ever configured
  to run nodes in SEPARATE PROCESSES, a child process gets its own memory and
  will not see the parent's updates. Therefore:
    - `update()` propagates to DOWNSTREAM (later-group) nodes, never to
      concurrent siblings in the same parallel group (which may be serialized /
      run elsewhere). Exports semantics respect this: publish for what comes
      after, not for what runs alongside.
    - A node takes a SNAPSHOT of the store when it starts, so it sees a stable
      view for its whole execution (determinism), while still picking up any
      upstream exports (the snapshot is taken at that node's start).
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from functools import lru_cache
from typing import Any


class RunContextService:
    """Thread-safe, run-scoped store of the open domain params.

    Holds a single mutable dict guarded by a lock. Reads are snapshots (a shallow
    copy), so a node/gate always works against a stable view; writes
    (`set`/`update`) are locked so concurrent exports do not corrupt the dict.

    The lock is a plain `threading.Lock` (not an anyio one) on purpose: it is held
    only for the microseconds of a dict copy/update and NEVER across an `await`,
    so it cannot stall the event loop, and it stays correct if a consumer's sync
    callable is offloaded to a worker thread.
    """

    def __init__(self, initial: Mapping[str, Any] | None = None) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, Any] = dict(initial or {})

    def get(self, key: str, default: Any = None) -> Any:
        """Read one param (default if absent)."""
        return self._data.get(key, default)

    def all(self) -> dict[str, Any]:
        """A SNAPSHOT (shallow copy) of the whole mapping — safe to read/template."""
        # dict() over the live dict is atomic enough for a shallow copy in CPython;
        # take the lock anyway to be correct under a concurrent update().
        with self._lock:
            return dict(self._data)

    # `snapshot` is an alias for `all` — used where the intent is "freeze the
    # view for this node's execution" rather than "read everything".
    snapshot = all

    def set(self, key: str, value: Any) -> None:
        """Set one param (locked)."""
        with self._lock:
            self._data[key] = value

    def update(self, mapping: Mapping[str, Any] | None = None, /, **kv: Any) -> None:
        """Merge keys into the store (locked). The exports-hook write path.

        Accepts either a mapping and/or keyword pairs. Later keys win. Intended
        for publishing values to DOWNSTREAM nodes (see module SCOPE note).
        """
        merged = {**(mapping or {}), **kv}
        if not merged:
            return
        with self._lock:
            self._data.update(merged)


# --- Global run-context lifecycle (lru_cache singleton, mirrors run_config) ---

_run_context_override: RunContextService | None = None


def init_run_context(params: Mapping[str, Any] | None = None) -> RunContextService:
    """Build the run-context service from the initial params and install it.

    Called once by the engine at flow start with the run's domain params. Returns
    the installed singleton so the caller can seed/read immediately.
    """
    global _run_context_override
    _run_context_override = RunContextService(params)
    _get_run_context_cached.cache_clear()
    return get_run_context()


def get_run_context() -> RunContextService:
    """Return the process-wide RunContextService (empty one if not init'd)."""
    return _get_run_context_cached()


@lru_cache(maxsize=1)
def _get_run_context_cached() -> RunContextService:
    return _run_context_override if _run_context_override is not None else RunContextService()


def clear_run_context() -> None:
    """Reset the run-context singleton — for testing / between runs."""
    global _run_context_override
    _run_context_override = None
    _get_run_context_cached.cache_clear()
