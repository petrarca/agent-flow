"""`maybe_await` — the single async/sync dispatch point.

Every consumer-supplied callable the engine invokes may be sync OR async: a
node's `run`, a gate, an export, and each observing hook. They all route through
this one helper. Miss a call site and async consumers silently break, so it
lives in its own module rather than beside any one caller.
"""

from __future__ import annotations

from inspect import isawaitable
from typing import Any


async def maybe_await(value: Any) -> Any:
    """Await `value` if it is awaitable, else return it as-is.

    The single dispatch point that makes every consumer callable additive: a
    node's `run`, a gate, an export, and observing hooks may each be sync OR
    async. Sync callables return a plain value (passed through); async ones
    return a coroutine (awaited here). Miss a call site and async consumers
    silently break — so ALL of them route through this.
    """
    if isawaitable(value):
        return await value
    return value
