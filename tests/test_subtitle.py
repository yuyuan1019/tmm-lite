"""Tests for provider fallback and subtitle destination handling."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.connection import Connection, LocalConnection
from app.scrapers.opensubtitles import SubtitleResult
from app.scrapers.subtitle import SubtitleDownloader, _subtitle_extension


class RecordingConnection(Connection):
    """Minimal remote-like connection that records writes without doing I/O."""

    def __init__(self, root: str) -> None:
        super().__init__(root)
        self.writes: list[tuple[str, bytes]] = []

    async def list_dir(self, path: str) -> list[str]:
        return []

    async def is_file(self, path: str) -> bool:
        return False

    async def is_dir(self, path: str) -> bool:
        return False

    async def read_bytes(self, path: str) -> bytes:
        raise FileNotFoundError(path)

    async def write_bytes(self, path: str, data: bytes) -> None:
        self.writes.append((path, data))

    async def mkdir(self, path: str, parents: bool = True) -> None:
        return None

    async def exists(self, path: str) -> bool:
        return False


def _result(
    provider: str = "subdl",
    *,
    language: str = "chi",
    filename: str = "release.srt",
) -> SubtitleResult:
    return SubtitleResult(provider, language, filename, "https://example.test/subtitle")


def _mock_subdl_download(
    monkeypatch: pytest.MonkeyPatch,
    downloader: SubtitleDownloader,
    data: bytes = b"subtitle-data",
) -> AsyncMock:
    subdl = AsyncMock()
    subdl.download.return_value = data
    monkeypatch.setattr(downloader, "_subdl", subdl)
    return subdl


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("release.srt", ".srt"),
        ("release.ASS", ".ass"),
        ("folder/release.ssa", ".ssa"),
        (r"folder\release.VTT", ".vtt"),
        ("release.txt", ".srt"),
        ("release", ".srt"),
    ],
)
def test_subtitle_extension(filename: str, expected: str) -> None:
    assert _subtitle_extension(filename) == expected


@pytest.mark.asyncio
async def test_save_connection_writes_beside_nested_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "movies"
    folder = root / "Film (2020)"
    folder.mkdir(parents=True)
    downloader = SubtitleDownloader("")
    _mock_subdl_download(monkeypatch, downloader, b"[Script Info]")
    result = _result(filename="release.ass")

    returned = await downloader._save(
        result,
        folder,
        "video.mkv",
        LocalConnection(str(root)),
    )

    assert returned == folder / "video.zh.ass"
    assert returned.read_bytes() == b"[Script Info]"
    assert not (root / "video.zh.ass").exists()


@pytest.mark.asyncio
async def test_save_connection_writes_beside_video_in_child_folder(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "movies"
    folder = root / "Film"
    (folder / "Disc").mkdir(parents=True)
    downloader = SubtitleDownloader("")
    _mock_subdl_download(monkeypatch, downloader, b"ssa-data")

    returned = await downloader._save(
        _result(filename="release.ssa"),
        folder,
        "Disc/video.mkv",
        LocalConnection(str(root)),
    )

    assert returned == folder / "Disc" / "video.zh.ssa"
    assert returned.read_bytes() == b"ssa-data"


@pytest.mark.asyncio
async def test_save_connection_handles_loose_video_at_library_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    downloader = SubtitleDownloader("")
    _mock_subdl_download(monkeypatch, downloader, b"WEBVTT")

    returned = await downloader._save(
        _result(filename="release.vtt"),
        root,
        "Loose (2020).mkv",
        LocalConnection(str(root)),
    )

    assert returned == root / "Loose (2020).zh.vtt"
    assert returned.read_bytes() == b"WEBVTT"


@pytest.mark.asyncio
async def test_save_unknown_extension_defaults_to_srt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "Film"
    downloader = SubtitleDownloader("")
    _mock_subdl_download(monkeypatch, downloader, b"srt-data")

    returned = await downloader._save(_result(filename="archive.zip"), folder, "video.mkv")

    assert returned == folder / "video.zh.srt"
    assert returned.read_bytes() == b"srt-data"


@pytest.mark.asyncio
async def test_save_remote_connection_receives_root_relative_posix_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("")
    _mock_subdl_download(monkeypatch, downloader, b"ass-data")
    connection = RecordingConnection("/media/movies")

    returned = await downloader._save(
        _result(filename="release.ass"),
        Path("/media/movies/Film"),
        "video.mkv",
        connection,
    )

    assert returned.as_posix() == "/media/movies/Film/video.zh.ass"
    assert connection.writes == [("Film/video.zh.ass", b"ass-data")]


@pytest.mark.asyncio
async def test_save_connection_rejects_destination_outside_root(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("")
    subdl = _mock_subdl_download(monkeypatch, downloader)
    connection = RecordingConnection("/media/movies")

    with pytest.raises(ValueError, match="connection root"):
        await downloader._save(
            _result(filename="release.ass"),
            Path("/media/other/Film"),
            "video.mkv",
            connection,
        )

    subdl.download.assert_not_awaited()
    assert connection.writes == []


@pytest.mark.asyncio
async def test_download_uses_assrt_result_first(monkeypatch: pytest.MonkeyPatch) -> None:
    downloader = SubtitleDownloader("os-key", assrt_token="assrt-token")
    result = _result("assrt")
    saved = Path("/saved/video.zh.srt")
    assrt_search = AsyncMock(return_value=result)
    os_search = AsyncMock()
    subdl_search = AsyncMock()
    save = AsyncMock(return_value=saved)
    monkeypatch.setattr(downloader, "_search_assrt", assrt_search)
    monkeypatch.setattr(downloader, "_search_os", os_search)
    monkeypatch.setattr(downloader, "_search_subdl", subdl_search)
    monkeypatch.setattr(downloader, "_save", save)

    returned = await downloader.download("Film", 2020, Path("/media/Film"), "video.mkv")

    assert returned == saved
    assrt_search.assert_awaited_once_with("Film", 2020, "chi,zho,zh")
    os_search.assert_not_awaited()
    subdl_search.assert_not_awaited()
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_falls_back_from_assrt_failure_to_opensubtitles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("os-key", assrt_token="assrt-token")
    result = _result("opensubtitles")
    assrt_search = AsyncMock(side_effect=RuntimeError("ASSRT unavailable"))
    os_search = AsyncMock(return_value=result)
    subdl_search = AsyncMock()
    save = AsyncMock(return_value=Path("/saved/video.zh.srt"))
    monkeypatch.setattr(downloader, "_search_assrt", assrt_search)
    monkeypatch.setattr(downloader, "_search_os", os_search)
    monkeypatch.setattr(downloader, "_search_subdl", subdl_search)
    monkeypatch.setattr(downloader, "_save", save)

    returned = await downloader.download("Film", 2020, Path("/media/Film"))

    assert returned == Path("/saved/video.zh.srt")
    assrt_search.assert_awaited_once()
    os_search.assert_awaited_once_with("Film", 2020, "chi,zho,zh", None)
    subdl_search.assert_not_awaited()
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_falls_back_from_opensubtitles_failure_to_subdl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("os-key")
    result = _result("subdl")
    os_search = AsyncMock(side_effect=RuntimeError("OpenSubtitles unavailable"))
    subdl_search = AsyncMock(return_value=result)
    save = AsyncMock(return_value=Path("/saved/video.zh.srt"))
    monkeypatch.setattr(downloader, "_search_os", os_search)
    monkeypatch.setattr(downloader, "_search_subdl", subdl_search)
    monkeypatch.setattr(downloader, "_save", save)

    returned = await downloader.download("Film", 2020, Path("/media/Film"))

    assert returned == Path("/saved/video.zh.srt")
    os_search.assert_awaited_once()
    subdl_search.assert_awaited_once_with("Film", 2020, "chi,zho,zh")
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_returns_none_when_all_providers_have_no_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("os-key", assrt_token="assrt-token")
    assrt_search = AsyncMock(return_value=None)
    os_search = AsyncMock(return_value=None)
    subdl_search = AsyncMock(return_value=None)
    save = AsyncMock()
    monkeypatch.setattr(downloader, "_search_assrt", assrt_search)
    monkeypatch.setattr(downloader, "_search_os", os_search)
    monkeypatch.setattr(downloader, "_search_subdl", subdl_search)
    monkeypatch.setattr(downloader, "_save", save)

    returned = await downloader.download("Missing", None, Path("/media/Missing"))

    assert returned is None
    assrt_search.assert_awaited_once()
    os_search.assert_awaited_once()
    subdl_search.assert_awaited_once()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_aclose_closes_and_resets_all_lazy_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("os-key", assrt_token="assrt-token")
    assrt = AsyncMock()
    opensubtitles = AsyncMock()
    subdl = AsyncMock()
    monkeypatch.setattr(downloader, "_assrt", assrt)
    monkeypatch.setattr(downloader, "_os", opensubtitles)
    monkeypatch.setattr(downloader, "_subdl", subdl)

    await downloader.aclose()

    assrt.aclose.assert_awaited_once_with()
    opensubtitles.aclose.assert_awaited_once_with()
    subdl.aclose.assert_awaited_once_with()
    assert downloader._assrt is None
    assert downloader._os is None
    assert downloader._subdl is None
