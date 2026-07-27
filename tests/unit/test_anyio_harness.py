"""Smoke test proving the anyio pytest harness works (Stage 1 of the async-first
migration). Once real async engine tests exist this file can be removed.
"""

import anyio
import pytest


@pytest.mark.anyio
async def test_anyio_marker_runs_on_asyncio():
    import sniffio

    assert sniffio.current_async_library() == "asyncio"


@pytest.mark.anyio
async def test_anyio_sleep_and_task_group():
    results: list[int] = []

    async def add(n: int) -> None:
        await anyio.sleep(0)
        results.append(n)

    async with anyio.create_task_group() as tg:
        for i in range(3):
            tg.start_soon(add, i)

    assert sorted(results) == [0, 1, 2]
