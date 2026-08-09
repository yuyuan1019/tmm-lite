"""Connection abstraction regression tests with no real network access."""

from __future__ import annotations

import asyncio
import io
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Self
from unittest.mock import patch

import httpx
import pytest

from app.connection import (
    ConnectionConfig,
    LocalConnection,
    SshConnection,
    WebdavConnection,
    _parse_propfind_entries,
    _resolve,
)


@pytest.mark.asyncio
async def test_local_connection_behavior_and_offloads(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "z.txt").write_bytes(b"0123456789")
    (root / "a-dir").mkdir()
    video_dir = root / "movie"
    video_dir.mkdir()
    (video_dir / "feature.mkv").write_bytes(b"video")
    conn = LocalConnection(str(root))

    real_to_thread = asyncio.to_thread
    with patch("app.connection.asyncio.to_thread", wraps=real_to_thread) as offload:
        assert await conn.list_dir("") == ["a-dir", "movie", "z.txt"]
        assert await conn.is_file("z.txt") is True
        assert await conn.is_dir("a-dir") is True
        assert await conn.read_bytes("z.txt") == b"0123456789"
        assert await conn.read_range("z.txt", 4, 3) == b"456"
        assert await conn.file_size("z.txt") == 10
        await conn.write_bytes("nested/out.bin", b"new")
        await conn.mkdir("created/deep")
        assert await conn.exists("created/deep") is True
        assert await conn.contains_video("movie") is True

    assert (root / "nested" / "out.bin").read_bytes() == b"new"
    assert not (root / "nested" / "out.bin.tmp").exists()
    assert (root / "created" / "deep").is_dir()
    assert offload.await_count >= 10


def test_local_path_resolution_accepts_relative_and_absolute_paths(tmp_path: Path) -> None:
    root = tmp_path / "root"
    absolute = tmp_path / "elsewhere.bin"
    assert _resolve(str(root), "folder/file.bin") == root / "folder" / "file.bin"
    assert _resolve(str(root), str(absolute)) == absolute


class _RecordingSftpFile:
    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)
        self.seeks: list[int] = []
        self.reads: list[int] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self._buffer.close()

    def seek(self, offset: int) -> int:
        self.seeks.append(offset)
        return self._buffer.seek(offset)

    def read(self, size: int) -> bytes:
        self.reads.append(size)
        return self._buffer.read(size)


class _FakeSftp:
    def __init__(self, data: bytes) -> None:
        self.data = data
        self.opened: list[tuple[str, str]] = []
        self.stat_calls: list[str] = []
        self.last_file: _RecordingSftpFile | None = None

    def open(self, path: str, mode: str) -> _RecordingSftpFile:
        self.opened.append((path, mode))
        self.last_file = _RecordingSftpFile(self.data)
        return self.last_file

    def stat(self, path: str) -> SimpleNamespace:
        self.stat_calls.append(path)
        return SimpleNamespace(st_size=len(self.data))


@pytest.mark.asyncio
async def test_sftp_read_range_is_bounded_and_size_uses_stat() -> None:
    conn = SshConnection("/root", ConnectionConfig(type="ssh", host="unused"))
    fake = _FakeSftp(b"0123456789")
    conn._sftp = fake

    assert await conn.read_range("Film/video.mkv", 5, 3) == b"567"
    assert await conn.file_size("Film/video.mkv") == 10

    assert fake.opened == [("/root/Film/video.mkv", "rb")]
    assert fake.last_file is not None
    assert fake.last_file.seeks == [5]
    assert fake.last_file.reads == [3]
    assert fake.stat_calls == ["/root/Film/video.mkv"]


def _multistatus(*responses: str) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:multistatus xmlns:d="DAV:">'
        + "".join(responses)
        + "</d:multistatus>"
    )


def _dav_response(href: str, *, collection: bool = False, size: int | None = None) -> str:
    resource_type = "<d:collection/>" if collection else ""
    length = f"<d:getcontentlength>{size}</d:getcontentlength>" if size is not None else ""
    return (
        "<d:response>"
        f"<d:href>{href}</d:href>"
        "<d:propstat><d:prop>"
        f"<d:resourcetype>{resource_type}</d:resourcetype>{length}"
        "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        "</d:response>"
    )


async def _webdav_connection(
    handler: Any,
    *,
    root: str = "/media",
    host: str = "https://nas.example:8443",
) -> WebdavConnection:
    conn = WebdavConnection(root, ConnectionConfig(type="webdav", host=host))
    await conn._client.aclose()
    conn._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return conn


@pytest.mark.asyncio
async def test_webdav_propfind_lists_decoded_entries_and_caches_resource_types() -> None:
    requests: list[httpx.Request] = []
    body = _multistatus(
        _dav_response("/media/", collection=True),
        _dav_response("https://nas.example:8443/media/Film%20One/", collection=True),
        _dav_response("/media/video.mkv"),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(207, text=body)

    conn = await _webdav_connection(handler)
    try:
        assert await conn.list_dir("") == ["Film One", "video.mkv"]
        assert await conn.is_dir("Film One") is True
        assert await conn.is_file("video.mkv") is True
        assert await conn.is_file("Film One") is False
        assert await conn.is_dir("video.mkv") is False
        assert conn._url("Film One/Part #1.mkv") == (
            "https://nas.example:8443/media/Film%20One/Part%20%231.mkv"
        )
    finally:
        await conn.aclose()

    assert len(requests) == 1
    assert requests[0].method == "PROPFIND"
    assert requests[0].headers["Depth"] == "1"
    assert str(requests[0].url) == "https://nas.example:8443/media"


@pytest.mark.asyncio
async def test_webdav_resource_type_cache_miss_uses_depth_zero_and_encoded_url() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        path = request.url.path
        if path.endswith("Uncached Folder"):
            return httpx.Response(207, text=_multistatus(_dav_response(path, collection=True)))
        if path.endswith("uncached.bin"):
            return httpx.Response(207, text=_multistatus(_dav_response(path)))
        return httpx.Response(404)

    conn = await _webdav_connection(handler)
    try:
        assert await conn.is_dir("Uncached Folder") is True
        assert await conn.is_file("Uncached Folder") is False
        assert await conn.is_file("uncached.bin") is True
        assert await conn.is_dir("uncached.bin") is False
        assert await conn.is_file("missing.bin") is False
    finally:
        await conn.aclose()

    assert [request.headers["Depth"] for request in requests] == ["0", "0", "0"]
    assert str(requests[0].url).endswith("/media/Uncached%20Folder")


@pytest.mark.asyncio
async def test_webdav_malformed_propfind_and_not_found_are_empty_or_false() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("broken"):
            return httpx.Response(207, text="<not-xml")
        return httpx.Response(404)

    conn = await _webdav_connection(handler)
    try:
        assert await conn.list_dir("broken") == []
        assert await conn.list_dir("missing") == []
        assert await conn.is_dir("missing") is False
        assert await conn.is_file("missing") is False
        assert await conn.exists("missing") is False
    finally:
        await conn.aclose()

    assert _parse_propfind_entries("<not-xml", "https://nas.example/media") == []


class _ChunkStream(httpx.AsyncByteStream):
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.yielded = 0
        self.closed = False

    async def __aiter__(self):  # type: ignore[no-untyped-def]
        for chunk in self.chunks:
            self.yielded += 1
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_webdav_range_read_is_bounded_and_closes_stream() -> None:
    stream = _ChunkStream([b"56789", b"unused"])
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(206, stream=stream)

    conn = await _webdav_connection(handler)
    try:
        assert await conn.read_range("Film One/video.mkv", 5, 3) == b"567"
        assert await conn.read_range("Film One/video.mkv", 0, 0) == b""
        with pytest.raises(ValueError):
            await conn.read_range("Film One/video.mkv", -1, 1)
    finally:
        await conn.aclose()

    assert len(requests) == 1
    assert requests[0].headers["Range"] == "bytes=5-7"
    assert requests[0].headers["Accept-Encoding"] == "identity"
    assert str(requests[0].url).endswith("/media/Film%20One/video.mkv")
    assert stream.yielded == 1
    assert stream.closed is True


@pytest.mark.asyncio
async def test_webdav_range_read_rejects_server_that_ignores_offset() -> None:
    stream = _ChunkStream([b"0123456789"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    conn = await _webdav_connection(handler)
    try:
        with pytest.raises(OSError, match="ignored"):
            await conn.read_range("video.mkv", 5, 3)
    finally:
        await conn.aclose()

    assert stream.yielded == 0
    assert stream.closed is True


@pytest.mark.asyncio
async def test_webdav_file_size_uses_head_or_propfind_length() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path.endswith("direct.mkv"):
            return httpx.Response(200, headers={"Content-Length": "123"})
        if request.url.path.endswith("fallback.mkv") and request.method == "HEAD":
            return httpx.Response(200, headers={"Transfer-Encoding": "chunked"})
        if request.url.path.endswith("fallback.mkv"):
            return httpx.Response(
                207,
                text=_multistatus(_dav_response(request.url.path, size=456)),
            )
        return httpx.Response(404)

    conn = await _webdav_connection(handler)
    try:
        assert await conn.file_size("direct.mkv") == 123
        assert await conn.file_size("fallback.mkv") == 456
        with pytest.raises(FileNotFoundError):
            await conn.file_size("missing.mkv")
    finally:
        await conn.aclose()

    propfind = [request for request in requests if request.method == "PROPFIND"]
    assert len(propfind) == 1
    assert propfind[0].headers["Depth"] == "0"
