"""Unit coverage for the async line-framing layer in core/agent_runtime.

anyio's `open_process` yields BYTE streams with no `text=True` and no line
iteration, so the supervisor reimplements universal-newline-ish framing in
`_iter_lines`. This is the "bytes not text" footgun — test it directly, without a
subprocess, feeding a fake byte stream chunk-by-chunk.
"""

import anyio
import pytest

from agent_flow.core.agent_runtime import _iter_lines


class _FakeByteStream:
    """A minimal async-iterable byte stream that emits preset chunks (bytes)."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        await anyio.sleep(0)
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


async def _collect(chunks: list[bytes]) -> list[str]:
    return [line async for line in _iter_lines(_FakeByteStream(chunks))]


@pytest.mark.anyio
async def test_splits_on_newline_keeping_terminator():
    lines = await _collect([b"a\nb\nc\n"])
    assert lines == ["a\n", "b\n", "c\n"]


@pytest.mark.anyio
async def test_reassembles_line_split_across_chunks():
    lines = await _collect([b"hel", b"lo\nwor", b"ld\n"])
    assert lines == ["hello\n", "world\n"]


@pytest.mark.anyio
async def test_yields_trailing_partial_line_at_eof():
    lines = await _collect([b"done\nno newline here"])
    assert lines == ["done\n", "no newline here"]


@pytest.mark.anyio
async def test_multibyte_utf8_split_across_chunk_boundary_is_replaced_not_crashed():
    # A 2-byte utf-8 char (é = \xc3\xa9) split across chunks: the framing must
    # never crash (errors="replace"); it may render as replacement chars but the
    # run survives, which is the invariant that matters for supervision.
    lines = await _collect([b"caf\xc3", b"\xa9\n"])
    assert len(lines) == 1
    assert lines[0].endswith("\n")


@pytest.mark.anyio
async def test_empty_stream_yields_nothing():
    assert await _collect([]) == []
