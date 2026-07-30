"""Unit coverage for the async line-framing layer in core/agent_runtime.

anyio's `open_process` yields BYTE streams with no `text=True` and no line
iteration, so the supervisor reimplements universal-newline-ish framing in
`_iter_lines`. This is the "bytes not text" footgun — test it directly, without a
subprocess, feeding a fake byte stream chunk-by-chunk.
"""

import anyio
import pytest

from agent_flow.runners.subprocess_exec import _iter_lines


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
async def test_multibyte_utf8_split_across_chunk_boundary_is_preserved():
    # A 2-byte utf-8 char (é = \xc3\xa9) split across chunks must be REASSEMBLED,
    # not corrupted: decoding is incremental, so the partial sequence is held
    # until its remaining bytes arrive. (A naive per-chunk bytes.decode() would
    # yield "caf\ufffd\ufffd".)
    assert await _collect([b"caf\xc3", b"\xa9\n"]) == ["café\n"]


@pytest.mark.anyio
async def test_invalid_bytes_degrade_to_replacement_not_crash():
    # A genuinely invalid byte still degrades to U+FFFD rather than raising —
    # a stray byte must never break supervision.
    assert await _collect([b"bad\xff byte\n"]) == ["bad\ufffd byte\n"]


@pytest.mark.anyio
async def test_dangling_partial_sequence_at_eof_is_flushed():
    # A truncated multi-byte sequence at EOF is flushed (as U+FFFD), not dropped.
    assert await _collect([b"x\n", b"\xc3"]) == ["x\n", "\ufffd"]


@pytest.mark.anyio
async def test_empty_stream_yields_nothing():
    assert await _collect([]) == []
