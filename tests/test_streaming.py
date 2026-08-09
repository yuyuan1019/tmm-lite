"""Strict byte-range parsing and bounded streaming tests."""

from __future__ import annotations

from typing import Any

import pytest

from app.streaming import (
    ByteRange,
    RangeNotSatisfiable,
    iter_connection_bytes,
    parse_byte_range,
)


@pytest.mark.parametrize(
    ("header", "size", "expected"),
    [
        (None, 10, None),
        ("bytes=0-3", 10, ByteRange(0, 3)),
        ("bytes=7-", 10, ByteRange(7, 9)),
        ("bytes=-4", 10, ByteRange(6, 9)),
        ("bytes=0-99", 10, ByteRange(0, 9)),
        ("bytes=-99", 10, ByteRange(0, 9)),
    ],
)
def test_parse_byte_range(
    header: str | None,
    size: int,
    expected: ByteRange | None,
) -> None:
    assert parse_byte_range(header, size) == expected


@pytest.mark.parametrize(
    "header",
    [
        "bytes=999-1000",
        "bytes=7-3",
        "bytes=0-1,4-5",
        "garbage",
        "bytes=-0",
        "bytes=-",
        "Bytes=0-1",
        "bytes= 0-1",
    ],
)
def test_parse_byte_range_rejects_invalid_or_unsupported_ranges(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_byte_range(header, 10)


@pytest.mark.parametrize("header", ["bytes=0-0", "bytes=0-", "bytes=-1"])
def test_parse_byte_range_rejects_every_range_for_an_empty_file(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_byte_range(header, 0)


def test_parse_byte_range_allows_full_empty_file_without_range() -> None:
    assert parse_byte_range(None, 0) is None


@pytest.mark.parametrize(
    "header",
    [
        "bytes=" + "9" * 5000 + "-",
        "bytes=-" + "9" * 5000,
    ],
    ids=["oversized-start", "oversized-suffix"],
)
def test_parse_byte_range_rejects_oversized_integers(header: str) -> None:
    with pytest.raises(RangeNotSatisfiable):
        parse_byte_range(header, 10)


class _FakeConnection:
    def __init__(self, data: bytes, *, oversized: bool = False) -> None:
        self.data = data
        self.oversized = oversized
        self.calls: list[tuple[str, int, int]] = []
        self.closed = False

    async def read_range(self, path: str, offset: int, size: int) -> bytes:
        self.calls.append((path, offset, size))
        extra = 2 if self.oversized else 0
        return self.data[offset:offset + size + extra]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_iter_connection_bytes_uses_bounded_inclusive_reads() -> None:
    conn = _FakeConnection(b"0123456789")

    chunks = [
        chunk
        async for chunk in iter_connection_bytes(
            conn,  # type: ignore[arg-type]
            "video.mkv",
            1,
            8,
            chunk_size=3,
        )
    ]

    assert chunks == [b"123", b"456", b"78"]
    assert b"".join(chunks) == b"12345678"
    assert conn.calls == [
        ("video.mkv", 1, 3),
        ("video.mkv", 4, 3),
        ("video.mkv", 7, 2),
    ]
    assert conn.closed is False


@pytest.mark.asyncio
async def test_iter_connection_bytes_trims_a_misbehaving_connection() -> None:
    conn = _FakeConnection(b"0123456789", oversized=True)
    result = b"".join(
        [
            chunk
            async for chunk in iter_connection_bytes(
                conn,  # type: ignore[arg-type]
                "video.mkv",
                0,
                4,
                chunk_size=3,
            )
        ],
    )
    assert result == b"01234"
    assert conn.calls == [("video.mkv", 0, 3), ("video.mkv", 3, 2)]


class _EmptyConnection:
    def __init__(self) -> None:
        self.calls = 0

    async def read_range(self, *args: Any) -> bytes:
        self.calls += 1
        return b""


@pytest.mark.asyncio
async def test_iter_connection_bytes_stops_on_empty_read() -> None:
    conn = _EmptyConnection()
    chunks = [
        chunk
        async for chunk in iter_connection_bytes(
            conn,  # type: ignore[arg-type]
            "short.mkv",
            0,
            9,
            chunk_size=2,
        )
    ]
    assert chunks == []
    assert conn.calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("start", "end", "chunk_size"),
    [(-1, 1, 1), (2, 1, 1), (0, 1, 0)],
)
async def test_iter_connection_bytes_rejects_invalid_bounds(
    start: int,
    end: int,
    chunk_size: int,
) -> None:
    conn = _FakeConnection(b"abc")
    with pytest.raises(ValueError):
        await anext(
            iter_connection_bytes(
                conn,  # type: ignore[arg-type]
                "video.mkv",
                start,
                end,
                chunk_size,
            ),
        )
    assert conn.calls == []
