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
        try:
            return sorted(
                child.name
                for child in p.iterdir()
                if child.is_dir()
            )
        except OSError:
            return []

    async def is_file(self, path: str) -> bool:
        return _resolve(self._root, path).is_file()

    async def is_dir(self, path: str) -> bool:
        return _resolve(self._root, path).is_dir()

    async def read_bytes(self, path: str) -> bytes:
        return _resolve(self._root, path).read_bytes()

    async def write_bytes(self, path: str, data: bytes) -> None:
        target = _resolve(self._root, path)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via temp file
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_bytes(data)
            tmp.replace(target)
        finally:
            if tmp.exists():
                tmp.unlink()

    async def mkdir(self, path: str, parents: bool = True) -> None:
        p = _resolve(self._root, path)
        p.mkdir(parents=parents, exist_ok=True)

    async def exists(self, path: str) -> bool:
        return _resolve(self._root, path).exists()

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
        base_url = f"http{'s' if config.port == 443 else ''}://{config.host}"
        if config.port and config.port not in (80, 443):
            base_url += f":{config.port}"
        self._base_url = base_url.rstrip("/")
        auth = (config.username, config.password) if config.username else None
        self._client = httpx.AsyncClient(
            auth=httpx.BasicAuth(*auth) if auth else None,  # type: ignore[arg-type]
            timeout=httpx.Timeout(30.0),
        )

    async def list_dir(self, path: str) -> list[str]:
        url = self._url(path)
        resp = await self._client.request("PROPFIND", url)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        # Simple XML parse (no extra deps) — we only need hrefs
        return _parse_propfind_hrefs(resp.text, url)

    async def is_file(self, path: str) -> bool:
        url = self._url(path)
        resp = await self._client.head(url)
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        content_type = resp.headers.get("content-type", "")
        # Directories typically have content-type that doesn't look like a file
        return "html" not in content_type and "directory" not in content_type

    async def is_dir(self, path: str) -> bool:
        url = self._url(path)
        resp = await self._client.head(url)
        if resp.status_code == 404:
            return False
        return resp.status_code < 400

    async def read_bytes(self, path: str) -> bytes:
        url = self._url(path)
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.content

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
        return resp.status_code < 400

    async def aclose(self) -> None:
        await self._client.aclose()

    def _url(self, path: str) -> str:
        """Build the full WebDAV URL for a path."""
        # Root path is prepended
        clean = (PurePosixPath(self._root) / path).as_posix().lstrip("/")
        return f"{self._base_url}/{clean}"


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


def _parse_propfind_hrefs(xml_text: str, base_url: str) -> list[str]:
    """Extract directory entry names from a WebDAV PROPFIND response."""
    import xml.etree.ElementTree as ET

    ns = {"d": "DAV:"}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    names: list[str] = []
    base = base_url.rstrip("/") + "/"
    for href in root.findall(".//d:href", ns):
        text = (href.text or "").strip()
        if not text:
            continue
        # Skip the directory itself
        if text.rstrip("/") == base.rstrip("/"):
            continue
        # Extract last path component
        name = text.rstrip("/").rsplit("/", 1)[-1]
        if name:
            names.append(name)
    return sorted(names)
