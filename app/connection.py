"""Connection abstraction layer for local and remote media libraries.

Provides a uniform async interface over local filesystems, SSH/SFTP,
and WebDAV.  SMB support is reserved for a future milestone.
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from app import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass
class ConnectionConfig:
    """Serialisable descriptor for a media library connection.

    ``type`` is ``"local"``, ``"ssh"``, ``"webdav"``, or ``"smb"``.
    For local connections only ``type`` is required.
    """

    type: str = "local"
    host: str = ""
    port: int = 0
    username: str = ""
    password: str = ""  # plaintext in memory; encrypted at rest
    extra: dict[str, str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------


class Connection(ABC):
    """Async filesystem-like interface for a media library root."""

    def __init__(self, root: str) -> None:
        self._root = root

    @property
    def root(self) -> str:
        return self._root

    @abstractmethod
    async def list_dir(self, path: str) -> list[str]:
        """Return **directory** children of *path* (absolute or relative to root)."""
        ...

    @abstractmethod
    async def is_file(self, path: str) -> bool:
        ...

    @abstractmethod
    async def is_dir(self, path: str) -> bool:
        ...

    @abstractmethod
    async def read_bytes(self, path: str) -> bytes:
        ...

    async def read_range(self, path: str, offset: int, size: int) -> bytes:
        """Read *size* bytes starting at *offset* from *path*.

        The default implementation reads the whole file and slices — override
        in subclasses for efficient chunked access.
        """
        data = await self.read_bytes(path)
        return data[offset:offset + size]

    async def file_size(self, path: str) -> int:
        """Return the size of the file at *path* in bytes."""
        data = await self.read_bytes(path)
        return len(data)

    @abstractmethod
    async def write_bytes(self, path: str, data: bytes) -> None:
        ...

    @abstractmethod
    async def mkdir(self, path: str, parents: bool = True) -> None:
        ...

    @abstractmethod
    async def exists(self, path: str) -> bool:
        ...

    async def contains_video(self, folder: str) -> bool:
        """Check if *folder* contains a video file (depth ≤2)."""
        try:
            children = await self.list_dir(folder)
            for child in children:
                child_path = str(PurePosixPath(folder) / child)
                if await self.is_file(child_path):
                    if Path(child).suffix.lower() in VIDEO_EXTENSIONS:
                        return True
                elif await self.is_dir(child_path):
                    subs = await self.list_dir(child_path)
                    for sub in subs:
                        sub_path = str(PurePosixPath(child_path) / sub)
                        if await self.is_file(sub_path) and Path(sub).suffix.lower() in VIDEO_EXTENSIONS:
                            return True
        except OSError:
            pass
        return False

    async def aclose(self) -> None:
        """Release any held resources (connections, sessions)."""


# ---------------------------------------------------------------------------
# Local filesystem
# ---------------------------------------------------------------------------


class LocalConnection(Connection):
    """Connection backed by the local filesystem via :mod:`pathlib`."""

    async def list_dir(self, path: str) -> list[str]:
        p = _resolve(self._root, path)
        return await asyncio.to_thread(lambda: sorted(child.name for child in p.iterdir()))

    async def is_file(self, path: str) -> bool:
        return await asyncio.to_thread(_resolve(self._root, path).is_file)

    async def is_dir(self, path: str) -> bool:
        return await asyncio.to_thread(_resolve(self._root, path).is_dir)

    async def read_bytes(self, path: str) -> bytes:
        return await asyncio.to_thread(_resolve(self._root, path).read_bytes)

    async def read_range(self, path: str, offset: int, size: int) -> bytes:
        p = _resolve(self._root, path)

        def _read() -> bytes:
            with p.open("rb") as file_obj:
                file_obj.seek(offset)
                return file_obj.read(size)

        return await asyncio.to_thread(_read)

    async def file_size(self, path: str) -> int:
        p = _resolve(self._root, path)
        return await asyncio.to_thread(lambda: p.stat().st_size)

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _resolve(self._root, path)

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            # Atomic write via temp file
            tmp = target.with_name(target.name + ".tmp")
            try:
                tmp.write_bytes(data)
                tmp.replace(target)
            finally:
                if tmp.exists():
                    tmp.unlink()

        await asyncio.to_thread(_write)

    async def mkdir(self, path: str, parents: bool = True) -> None:
        p = _resolve(self._root, path)
        await asyncio.to_thread(p.mkdir, parents=parents, exist_ok=True)

    async def exists(self, path: str) -> bool:
        return await asyncio.to_thread(_resolve(self._root, path).exists)

    async def contains_video(self, folder: str) -> bool:
        from app.scanner import contains_video  # avoid circular import
        return await asyncio.to_thread(contains_video, _resolve(self._root, folder))

    async def aclose(self) -> None:
        pass


# ---------------------------------------------------------------------------
# SSH / SFTP
# ---------------------------------------------------------------------------


class SshConnection(Connection):
    """Connection over SSH/SFTP using :mod:`paramiko`."""

    def __init__(self, root: str, config: ConnectionConfig) -> None:
        super().__init__(root)
        self._config = config
        self._client: Any = None
        self._sftp: Any = None

    async def _ensure_connected(self) -> None:
        if self._sftp is not None:
            return
        import paramiko

        def _connect() -> None:
            port = self._config.port or 22
            self._client = paramiko.SSHClient()
            self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            self._client.connect(
                hostname=self._config.host,
                port=port,
                username=self._config.username,
                password=self._config.password or None,
                timeout=15,
            )
            self._sftp = self._client.open_sftp()

        await asyncio.to_thread(_connect)

    async def list_dir(self, path: str) -> list[str]:
        await self._ensure_connected()
        assert self._sftp is not None

        def _list() -> list[str]:
            remote_path = str(PurePosixPath(self._root) / path)
            result: list[str] = []
            for attr in self._sftp.listdir_attr(remote_path):
                import stat
                if stat.S_ISDIR(attr.st_mode) or stat.S_ISREG(attr.st_mode):
                    pass  # include both; caller filters
                result.append(attr.filename)
            return sorted(result)

        return await asyncio.to_thread(_list)

    async def is_file(self, path: str) -> bool:
        await self._ensure_connected()
        assert self._sftp is not None
        remote = str(PurePosixPath(self._root) / path)
        import stat
        try:
            attr = await asyncio.to_thread(self._sftp.stat, remote)
            return stat.S_ISREG(attr.st_mode)
        except FileNotFoundError:
            return False

    async def is_dir(self, path: str) -> bool:
        await self._ensure_connected()
        assert self._sftp is not None
        remote = str(PurePosixPath(self._root) / path)
        import stat
        try:
            attr = await asyncio.to_thread(self._sftp.stat, remote)
            return stat.S_ISDIR(attr.st_mode)
        except FileNotFoundError:
            return False

    async def read_bytes(self, path: str) -> bytes:
        await self._ensure_connected()
        assert self._sftp is not None
        remote = str(PurePosixPath(self._root) / path)
        return await asyncio.to_thread(self._read_bytes, remote)

    async def read_range(self, path: str, offset: int, size: int) -> bytes:
        await self._ensure_connected()
        assert self._sftp is not None
        remote = str(PurePosixPath(self._root) / path)

        def _read() -> bytes:
            assert self._sftp is not None
            with self._sftp.open(remote, "rb") as file_obj:
                file_obj.seek(offset)
                return file_obj.read(size)

        return await asyncio.to_thread(_read)

    async def file_size(self, path: str) -> int:
        await self._ensure_connected()
        assert self._sftp is not None
        remote = str(PurePosixPath(self._root) / path)
        attr = await asyncio.to_thread(self._sftp.stat, remote)
        return int(attr.st_size)

    def _read_bytes(self, remote: str) -> bytes:
        assert self._sftp is not None
        with self._sftp.open(remote, "rb") as f:
            return f.read()

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self._ensure_connected()
        assert self._sftp is not None
        remote = str(PurePosixPath(self._root) / path)
        tmp = remote + ".tmp"

        def _write() -> None:
            assert self._sftp is not None
            # Ensure parent dir
            parent = str(PurePosixPath(remote).parent)
            try:
                self._sftp.stat(parent)
            except FileNotFoundError:
                self._mkdir_p(parent)
            with self._sftp.open(tmp, "wb") as f:
                f.write(data)
            self._sftp.posix_rename(tmp, remote)

        await asyncio.to_thread(_write)

    async def mkdir(self, path: str, parents: bool = True) -> None:
        await self._ensure_connected()
        assert self._sftp is not None
        remote = str(PurePosixPath(self._root) / path)
        if parents:
            await asyncio.to_thread(self._mkdir_p, remote)
        else:
            await asyncio.to_thread(self._sftp.mkdir, remote)

    def _mkdir_p(self, remote: str) -> None:
        assert self._sftp is not None
        parts = PurePosixPath(remote).parts
        current = "/"
        for part in parts[1:]:  # skip root "/"
            current = str(PurePosixPath(current) / part)
            try:
                self._sftp.stat(current)
            except FileNotFoundError:
                self._sftp.mkdir(current)

    async def exists(self, path: str) -> bool:
        await self._ensure_connected()
        assert self._sftp is not None
        remote = str(PurePosixPath(self._root) / path)
        try:
            await asyncio.to_thread(self._sftp.stat, remote)
            return True
        except FileNotFoundError:
            return False

    async def aclose(self) -> None:
        if self._sftp is not None:
            await asyncio.to_thread(self._sftp.close)
            self._sftp = None
        if self._client is not None:
            await asyncio.to_thread(self._client.close)
            self._client = None


# ---------------------------------------------------------------------------
# WebDAV
# ---------------------------------------------------------------------------


class WebdavConnection(Connection):
    """Connection over WebDAV (HTTP) using :mod:`httpx`."""

    def __init__(self, root: str, config: ConnectionConfig) -> None:
        super().__init__(root)
        host = config.host.strip().rstrip("/")
        if host.lower().startswith(("http://", "https://")):
            base_url = host
        else:
            scheme = "https" if config.port == 443 else "http"
            base_url = f"{scheme}://{host}"
            if config.port and config.port not in (80, 443):
                base_url += f":{config.port}"
        self._base_url = base_url.rstrip("/")
        self._resource_types: dict[str, bool] = {}
        auth = (config.username, config.password) if config.username else None
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(*auth) if auth else None,  # type: ignore[arg-type]
            timeout=httpx.Timeout(30.0),
        )

    async def list_dir(self, path: str) -> list[str]:
        url = self._url(path)
        resp = await self._client.request("PROPFIND", url, headers={"Depth": "1"})
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        entries = _parse_propfind_entries(resp.text, url)
        self._resource_types[_resource_key(url)] = True
        for name, is_collection in entries:
            child_path = str(PurePosixPath(path) / name)
            self._resource_types[_resource_key(self._url(child_path))] = is_collection
        return [name for name, _ in entries]

    async def is_file(self, path: str) -> bool:
        resource_type = await self._resource_type(path)
        return resource_type is False

    async def is_dir(self, path: str) -> bool:
        resource_type = await self._resource_type(path)
        return resource_type is True

    async def read_bytes(self, path: str) -> bytes:
        url = self._url(path)
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.content

    async def read_range(self, path: str, offset: int, size: int) -> bytes:
        if offset < 0 or size < 0:
            raise ValueError("offset and size must be non-negative")
        if size == 0:
            return b""

        request = self._client.build_request(
            "GET",
            self._url(path),
            headers={
                "Range": f"bytes={offset}-{offset + size - 1}",
                "Accept-Encoding": "identity",
            },
        )
        response = await self._client.send(request, stream=True)
        try:
            response.raise_for_status()
            if offset > 0 and response.status_code != 206:
                raise OSError("WebDAV server ignored the byte-range request")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                remaining = size - len(data)
                if remaining <= 0:
                    break
                data.extend(chunk[:remaining])
                if len(data) >= size:
                    break
            return bytes(data)
        finally:
            await response.aclose()

    async def file_size(self, path: str) -> int:
        url = self._url(path)
        response = await self._client.head(url)
        if response.status_code == 404:
            raise FileNotFoundError(path)
        if response.status_code not in (405, 501):
            response.raise_for_status()
            content_length = _non_negative_int(response.headers.get("content-length"))
            if content_length is not None:
                return content_length

        response = await self._client.request("PROPFIND", url, headers={"Depth": "0"})
        if response.status_code == 404:
            raise FileNotFoundError(path)
        response.raise_for_status()
        properties = _parse_propfind_properties(response.text, url)
        if properties is None or properties[1] is None:
            raise OSError(f"WebDAV server did not report a size for {path!r}")
        self._resource_types[_resource_key(url)] = properties[0]
        return properties[1]

    async def write_bytes(self, path: str, data: bytes) -> None:
        url = self._url(path)
        resp = await self._client.put(url, content=data)
        resp.raise_for_status()

    async def mkdir(self, path: str, parents: bool = True) -> None:
        if parents:
            # Create intermediate dirs
            parts = PurePosixPath(path).parts
            for i in range(1, len(parts) + 1):
                sub = str(PurePosixPath("").joinpath(*parts[:i]))
                await self._mkcol(sub)
        else:
            await self._mkcol(path)

    async def _mkcol(self, path: str) -> None:
        url = self._url(path)
        resp = await self._client.request("MKCOL", url)
        if resp.status_code in (201, 405):  # 405 = already exists
            return
        resp.raise_for_status()

    async def exists(self, path: str) -> bool:
        url = self._url(path)
        resp = await self._client.head(url)
        if resp.status_code == 404:
            return False
        if resp.status_code < 400:
            return True
        if resp.status_code not in (405, 501):
            resp.raise_for_status()
        resp = await self._client.request("PROPFIND", url, headers={"Depth": "0"})
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        """Build the full WebDAV URL for a path."""
        candidate = PurePosixPath(path)
        joined = candidate if candidate.is_absolute() else PurePosixPath(self._root) / candidate
        parts = [quote(part, safe="") for part in joined.parts if part not in ("/", ".")]
        encoded_path = "/".join(parts)
        return f"{self._base_url}/{encoded_path}" if encoded_path else f"{self._base_url}/"

    async def _resource_type(self, path: str) -> bool | None:
        url = self._url(path)
        key = _resource_key(url)
        if key in self._resource_types:
            return self._resource_types[key]

        response = await self._client.request("PROPFIND", url, headers={"Depth": "0"})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        properties = _parse_propfind_properties(response.text, url)
        if properties is None:
            return None
        is_collection, _ = properties
        self._resource_types[key] = is_collection
        return is_collection


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def create_connection(config: ConnectionConfig, root: str) -> Connection:
    """Create the appropriate :class:`Connection` for *config*."""
    if config.type == "ssh":
        return SshConnection(root, config)
    elif config.type == "webdav":
        return WebdavConnection(root, config)
    elif config.type == "smb":
        raise NotImplementedError("SMB connection not yet implemented")
    else:
        return LocalConnection(root)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _resolve(root: str, path: str) -> Path:
    """Resolve *path* against *root*."""
    p = Path(path)
    if p.is_absolute():
        return p
    return Path(root) / path


def _parse_propfind_entries(xml_text: str, request_url: str) -> list[tuple[str, bool]]:
    """Extract direct child names and collection flags from PROPFIND XML."""
    resources = _parse_propfind_resources(xml_text)
    request_path = _resource_key(request_url)
    entries: dict[str, bool] = {}
    for href, is_collection, _ in resources:
        resolved = urljoin(request_url.rstrip("/") + "/", href)
        if _resource_key(resolved) == request_path:
            continue
        raw_path = urlsplit(resolved).path.rstrip("/")
        raw_name = raw_path.rsplit("/", 1)[-1]
        if raw_name:
            entries[unquote(raw_name)] = is_collection
    return sorted(entries.items())


def _parse_propfind_properties(
    xml_text: str, request_url: str,
) -> tuple[bool, int | None] | None:
    """Return the collection flag and optional size for one PROPFIND target."""
    resources = _parse_propfind_resources(xml_text)
    if not resources:
        return None

    request_path = _resource_key(request_url)
    for href, is_collection, size in resources:
        resolved = urljoin(request_url.rstrip("/") + "/", href)
        if _resource_key(resolved) == request_path:
            return is_collection, size

    # Some servers return a relative or non-canonical href for Depth: 0.
    if len(resources) == 1:
        _, is_collection, size = resources[0]
        return is_collection, size
    return None


def _parse_propfind_resources(xml_text: str) -> list[tuple[str, bool, int | None]]:
    """Parse href, resource type, and content length from a multistatus body."""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    resources: list[tuple[str, bool, int | None]] = []
    for response in root.findall(".//{DAV:}response"):
        href = response.find("{DAV:}href")
        href_text = (href.text or "").strip() if href is not None else ""
        if not href_text:
            continue
        is_collection = response.find(
            ".//{DAV:}resourcetype/{DAV:}collection",
        ) is not None
        length_element = response.find(".//{DAV:}getcontentlength")
        size = _non_negative_int(length_element.text if length_element is not None else None)
        resources.append((href_text, is_collection, size))
    return resources


def _resource_key(url: str) -> str:
    """Return a stable, decoded URL-path key for the resource-type cache."""
    path = unquote(urlsplit(url).path).rstrip("/")
    return path or "/"


def _non_negative_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None
