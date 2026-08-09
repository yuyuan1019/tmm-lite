"""Helpers for strict HTTP byte-range parsing and bounded media streaming."""

from __future__ import annotations

import re
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.connection import Connection

_SINGLE_BYTE_RANGE = re.compile(r"bytes=([0-9]*)-([0-9]*)")


@dataclass(frozen=True, slots=True)
class ByteRange:
    """An inclusive byte range."""

    start: int
    end: int


class RangeNotSatisfiable(ValueError):
    """Raised when an HTTP Range header cannot select bytes from a file."""


def _parse_decimal(value: str) -> int:
    """Parse a range integer while normalising Python's digit-limit error."""
    try:
        return int(value)
    except ValueError as exc:
        raise RangeNotSatisfiable("range integer is too large") from exc


def parse_byte_range(header: str | None, file_size: int) -> ByteRange | None:
    """Parse one ``bytes`` range and clamp its end to the available file.

    Multiple ranges are deliberately unsupported.  A missing header means the
    caller should return the complete representation.
    """
    if header is None:
        return None
    if file_size <= 0:
        raise RangeNotSatisfiable("byte ranges require a non-empty file")

    match = _SINGLE_BYTE_RANGE.fullmatch(header.strip())
    if match is None:
        raise RangeNotSatisfiable("expected one bytes range")

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        raise RangeNotSatisfiable("range bounds are empty")

    if not start_text:
        suffix_length = _parse_decimal(end_text)
        if suffix_length <= 0:
            raise RangeNotSatisfiable("suffix length must be positive")
        return ByteRange(max(file_size - suffix_length, 0), file_size - 1)

    start = _parse_decimal(start_text)
    if start >= file_size:
        raise RangeNotSatisfiable("range starts beyond the file")

    if not end_text:
        return ByteRange(start, file_size - 1)

    end = _parse_decimal(end_text)
    if end < start:
        raise RangeNotSatisfiable("range end precedes its start")
    return ByteRange(start, min(end, file_size - 1))


async def iter_connection_bytes(
    conn: Connection,
    path: str,
    start: int,
    end: int,
    chunk_size: int = 1024 * 1024,
) -> AsyncIterator[bytes]:
    """Yield an inclusive connection range through bounded read calls.

    The caller owns *conn* and remains responsible for closing it.
    """
    if start < 0:
        raise ValueError("start must be non-negative")
    if end < start:
        raise ValueError("end must not precede start")
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    position = start
    while position <= end:
        requested = min(chunk_size, end - position + 1)
        chunk = await conn.read_range(path, position, requested)
        if not chunk:
            break
        bounded = chunk[:requested]
        if not bounded:
            break
        yield bounded
        position += len(bounded)
