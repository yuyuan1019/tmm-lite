"""Tests for provider fallback and subtitle destination handling."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path
from unittest.mock import AsyncMock
from zipfile import ZipFile

import pytest

from app.connection import Connection, LocalConnection
from app.exceptions import SubtitleError
from app.scrapers.opensubtitles import SubtitleResult
from app.scrapers.subtitle import (
    SubtitleDownloader,
    _opensubtitles_languages,
    _subdl_languages,
    _subtitle_extension,
    _subtitle_suffix,
)
from app.scrapers.subtitle_language import (
    chinese_variant,
    contains_chinese_text,
    expects_chinese,
    filename_language_score,
    preferred_variant,
)

_CHINESE_TEXT = "这是中文字幕正文，用于确认下载内容确实包含足够多的中文，而不是仅相信供应商语言标签。"


def _subtitle_bytes(prefix: str = "") -> bytes:
    return f"{prefix}\n{_CHINESE_TEXT}\n{_CHINESE_TEXT}".encode()


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
    data: bytes | None = None,
) -> AsyncMock:
    subdl = AsyncMock()
    subdl.download.return_value = data if data is not None else _subtitle_bytes()
    monkeypatch.setattr(downloader, "_subdl", subdl)
    return subdl


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("release.srt", ".srt"),
        ("release.ASS", ".ass"),
        ("folder/release.ssa", ".ssa"),
        (r"folder\release.VTT", ".vtt"),
        ("release.SUB", ".sub"),
        ("release.txt", ".srt"),
        ("release", ".srt"),
    ],
)
def test_subtitle_extension(filename: str, expected: str) -> None:
    assert _subtitle_extension(filename) == expected


def test_provider_language_mapping_keeps_legacy_chinese_compatible() -> None:
    assert _opensubtitles_languages(["chi", "zho", "zh"]) == "zh-cn,zh-tw,ze"
    assert _opensubtitles_languages(["zh-cn"]) == "zh-cn"
    assert _subdl_languages(["chi", "zh-tw", "eng"]) == "ZH-Hans,ZH-Hant,EN"
    assert _subdl_languages(["zh-cn"]) == "ZH-Hans"
    assert _subdl_languages(["zh-tw"]) == "ZH-Hant"
    assert _subdl_languages(["zh-hans"]) == "ZH-Hans"
    assert _subdl_languages(["zh-hant"]) == "ZH-Hant"
    assert _subtitle_suffix("中英双语") == "zh"


def test_chinese_content_detection_handles_common_encodings_and_rejects_neighbors() -> None:
    simplified = _CHINESE_TEXT * 2
    traditional = "這是繁體中文字幕，用來確認內容確實是中文，而不是日文或韓文字幕。" * 2
    japanese = "これは日本語の字幕です。映画の内容を説明するための文章が続きます。" * 4
    korean = "이것은 한국어 자막이며 영화 내용을 설명하는 문장이 계속됩니다." * 4

    assert contains_chinese_text(simplified.encode())
    assert contains_chinese_text(simplified.encode("gb18030"))
    assert contains_chinese_text(traditional.encode("big5"))
    assert contains_chinese_text(simplified.encode("utf-16"))
    assert not contains_chinese_text(japanese.encode())
    assert not contains_chinese_text(japanese.encode("cp932"))
    assert not contains_chinese_text(korean.encode())
    assert not contains_chinese_text(korean.encode("cp949"))
    assert not contains_chinese_text(b"English subtitle only")


def test_filename_language_ranking_respects_chinese_variant_preference() -> None:
    assert expects_chinese(["zh-cn"])
    assert not expects_chinese(["en"])
    assert filename_language_score("movie.简体.chs.srt", ["zh-cn"]) > filename_language_score(
        "movie.繁体.cht.srt", ["zh-cn"]
    )
    assert filename_language_score("movie.繁体.cht.srt", ["zh-tw"]) > filename_language_score(
        "movie.简体.chs.srt", ["zh-tw"]
    )
    assert filename_language_score("movie.english.srt", ["zh-cn"]) < 0
    assert filename_language_score("movie.english.srt", ["en"]) == 0


def test_filename_language_ranking_prefers_bilingual_for_english_movies() -> None:
    # Bilingual (中英对照) matching the preferred variant ranks above
    # Chinese-only — useful for English movies.
    assert filename_language_score("movie.中英双语.chs.srt", ["zh-cn"]) > filename_language_score(
        "movie.简体.chs.srt", ["zh-cn"]
    )
    assert filename_language_score("movie.chs-en.srt", ["zh-cn"]) > filename_language_score(
        "movie.chs.srt", ["zh-cn"]
    )
    assert filename_language_score("movie.zh-tw.en.srt", ["zh-tw"]) > filename_language_score(
        "movie.cht.srt", ["zh-tw"]
    )
    # Bilingual of the *opposite* variant is not preferred over the right one.
    assert filename_language_score("movie.简体.chs.srt", ["zh-cn"]) > filename_language_score(
        "movie.cht-en.srt", ["zh-cn"]
    )


_TRADITIONAL_TEXT = "這是繁體中文字幕正文，用來確認下載內容確實是繁體中文，而不是簡體中文或日文韓文字幕。"


def test_chinese_variant_detects_content_variant() -> None:
    simplified = _CHINESE_TEXT * 2
    traditional = _TRADITIONAL_TEXT * 2
    assert chinese_variant(simplified.encode()) == "simplified"
    assert chinese_variant(simplified.encode("gb18030")) == "simplified"
    assert chinese_variant(traditional.encode()) == "traditional"
    assert chinese_variant(traditional.encode("big5")) == "traditional"
    # Not enough distinguishing characters -> ambiguous
    assert chinese_variant(_CHINESE_TEXT[:1].encode()) in (None, "simplified")


def test_preferred_variant_defaults_to_simplified() -> None:
    assert preferred_variant(["zh-cn"]) == "simplified"
    assert preferred_variant(["zh-hans"]) == "simplified"
    assert preferred_variant(["zh"]) == "simplified"
    assert preferred_variant(["chi"]) == "simplified"
    assert preferred_variant(["zh-tw"]) == "traditional"
    assert preferred_variant(["zh-hant"]) == "traditional"
    assert preferred_variant(["en"]) is None


@pytest.mark.asyncio
async def test_save_rejects_traditional_subtitle_when_simplified_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("", subdl_api_key="subdl-key")
    _mock_subdl_download(
        monkeypatch,
        downloader,
        _TRADITIONAL_TEXT.encode(),
    )

    with pytest.raises(SubtitleError, match="繁体中文"):
        await downloader._save(_result(language="zh-cn"), tmp_path, "video.mkv")

    assert not (tmp_path / "video.zh.srt").exists()


@pytest.mark.asyncio
async def test_save_accepts_simplified_subtitle_when_simplified_requested(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("", subdl_api_key="subdl-key")
    _mock_subdl_download(monkeypatch, downloader, _CHINESE_TEXT.encode())

    returned = await downloader._save(_result(language="zh-cn"), tmp_path, "video.mkv")

    assert returned == tmp_path / "video.zh.srt"
    assert returned.read_bytes() == _CHINESE_TEXT.encode()


@pytest.mark.asyncio
async def test_save_connection_writes_beside_nested_video(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "movies"
    folder = root / "Film (2020)"
    folder.mkdir(parents=True)
    downloader = SubtitleDownloader("")
    payload = _subtitle_bytes("[Script Info]")
    _mock_subdl_download(monkeypatch, downloader, payload)
    result = _result(filename="release.ass")

    returned = await downloader._save(
        result,
        folder,
        "video.mkv",
        LocalConnection(str(root)),
    )

    assert returned == folder / "video.zh.ass"
    assert returned.read_bytes() == payload
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
    payload = _subtitle_bytes("[Script Info]")
    _mock_subdl_download(monkeypatch, downloader, payload)

    returned = await downloader._save(
        _result(filename="release.ssa"),
        folder,
        "Disc/video.mkv",
        LocalConnection(str(root)),
    )

    assert returned == folder / "Disc" / "video.zh.ssa"
    assert returned.read_bytes() == payload


@pytest.mark.asyncio
async def test_save_connection_handles_loose_video_at_library_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "movies"
    root.mkdir()
    downloader = SubtitleDownloader("")
    payload = _subtitle_bytes("WEBVTT")
    _mock_subdl_download(monkeypatch, downloader, payload)

    returned = await downloader._save(
        _result(filename="release.vtt"),
        root,
        "Loose (2020).mkv",
        LocalConnection(str(root)),
    )

    assert returned == root / "Loose (2020).zh.vtt"
    assert returned.read_bytes() == payload


@pytest.mark.asyncio
async def test_save_unknown_extension_defaults_to_srt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    folder = tmp_path / "Film"
    downloader = SubtitleDownloader("")
    payload = _subtitle_bytes()
    _mock_subdl_download(monkeypatch, downloader, payload)

    returned = await downloader._save(_result(filename="archive.zip"), folder, "video.mkv")

    assert returned == folder / "video.zh.srt"
    assert returned.read_bytes() == payload


@pytest.mark.asyncio
async def test_save_remote_connection_receives_root_relative_posix_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("")
    payload = _subtitle_bytes("[Script Info]")
    _mock_subdl_download(monkeypatch, downloader, payload)
    connection = RecordingConnection("/media/movies")

    returned = await downloader._save(
        _result(filename="release.ass"),
        Path("/media/movies/Film"),
        "video.mkv",
        connection,
    )

    assert returned.as_posix() == "/media/movies/Film/video.zh.ass"
    assert connection.writes == [("Film/video.zh.ass", payload)]


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
    assrt_search = AsyncMock(return_value=[result])
    os_search = AsyncMock()
    subdl_search = AsyncMock()
    save = AsyncMock(return_value=saved)
    monkeypatch.setattr(downloader, "_search_assrt", assrt_search)
    monkeypatch.setattr(downloader, "_search_os", os_search)
    monkeypatch.setattr(downloader, "_search_subdl", subdl_search)
    monkeypatch.setattr(downloader, "_save", save)

    returned = await downloader.download("Film", 2020, Path("/media/Film"), "video.mkv")

    assert returned == saved
    assrt_search.assert_awaited_once_with("Film", 2020, "zh-cn")
    os_search.assert_not_awaited()
    subdl_search.assert_not_awaited()
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_reports_provider_steps_via_progress_callback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Downloader emits provider-choice and candidate details on progress."""
    messages: list[str] = []
    downloader = SubtitleDownloader(
        "os-key", assrt_token="assrt-token", subdl_api_key="subdl-key",
        on_progress=messages.append,
    )
    assrt_result = _result("assrt", filename="movie.简体.chs.srt")
    save = AsyncMock(return_value=Path("/saved/video.zh.srt"))
    monkeypatch.setattr(downloader, "_search_assrt", AsyncMock(return_value=[assrt_result]))
    monkeypatch.setattr(downloader, "_save", save)

    await downloader.download("Film", 2020, Path("/media/Film"), "video.mkv")

    joined = "\n".join(messages)
    assert "尝试字幕源 ASSRT" in joined
    assert "ASSRT 命中候选: movie.简体.chs.srt" in joined
    # SubDL was configured but ASSRT succeeded, so it must not be attempted
    assert "SubDL" not in joined


@pytest.mark.asyncio
async def test_download_tries_next_candidate_after_variant_rejection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A traditional candidate is skipped; the next simplified one wins."""
    downloader = SubtitleDownloader("os-key", assrt_token="assrt-token")
    trad = _result("assrt", filename="movie.cht.srt")
    simp = _result("assrt", filename="movie.chs.srt")
    monkeypatch.setattr(downloader, "_search_assrt", AsyncMock(return_value=[trad, simp]))
    saves: list[str] = []

    async def fake_save(result, folder, video_filename=None, connection=None):
        payload = result.filename
        if "cht" in payload:
            raise SubtitleError("返回的字幕为繁体中文，不符合要求的简体中文")
        saves.append(payload)
        return Path("/saved") / payload

    monkeypatch.setattr(downloader, "_save", fake_save)

    returned = await downloader.download("Film", 2020, tmp_path, "video.mkv")

    assert returned == Path("/saved/movie.chs.srt")
    assert saves == ["movie.chs.srt"]


@pytest.mark.asyncio
async def test_search_os_prefers_simplified_candidate_by_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """OpenSubtitles picks the simplified-marked file over a traditional one."""
    downloader = SubtitleDownloader("os-key")
    os = AsyncMock()
    os.search.return_value = [
        _result("opensubtitles", filename="movie.cht.srt"),
        _result("opensubtitles", filename="movie.chs.srt"),
    ]
    monkeypatch.setattr(downloader, "_os", os)

    picked = await downloader._search_os("Movie", 2020, "zh-cn", None)

    assert [r.filename for r in picked] == ["movie.chs.srt", "movie.cht.srt"]


@pytest.mark.asyncio
async def test_search_subdl_prefers_simplified_candidate_by_filename(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SubDL picks the simplified-marked file over a traditional one."""
    downloader = SubtitleDownloader("", subdl_api_key="subdl-key")
    subdl = AsyncMock()
    subdl.search.return_value = [
        _result("subdl", filename="movie.繁体.ass"),
        _result("subdl", filename="movie.简体.srt"),
    ]
    monkeypatch.setattr(downloader, "_subdl", subdl)

    picked = await downloader._search_subdl("Movie", 2020, "ZH-Hans", None)

    assert [r.filename for r in picked] == ["movie.简体.srt", "movie.繁体.ass"]


def test_filename_release_similarity_prefers_matching_version() -> None:
    from app.scrapers.subtitle import filename_release_similarity

    video = "Movie (2020) UHD BluRay 2160p HEVC Atmos TrueHD7.1-MTeam.mkv"
    wrong = "Movie (2020) iNTERNAL 1080p WEBRip x265 HEVC-PSA-chs-en.srt"
    right = "Movie.2020.2160p.UHD.BluRay.HEVC.Atmos.TrueHD.7.1-MTeam.chs-en.srt"
    # Same release family scores far higher than a different source/res.
    assert filename_release_similarity(video, right) > 0.5
    assert filename_release_similarity(video, wrong) < 0.5
    # No recognizable features on either side -> neutral.
    assert filename_release_similarity(video, "Movie (2020).srt") == 0.0
    assert filename_release_similarity(None, right) == 0.0


@pytest.mark.asyncio
async def test_search_subdl_prefers_matching_release_version(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """SubDL picks the candidate whose release matches the video file."""
    downloader = SubtitleDownloader("", subdl_api_key="subdl-key")
    subdl = AsyncMock()
    subdl.search.return_value = [
        _result("subdl", filename="Movie.2020.1080p.WEBRip.PSA.chs.srt"),
        _result("subdl", filename="Movie.2020.2160p.BluRay.MTeam.chs.srt"),
    ]
    monkeypatch.setattr(downloader, "_subdl", subdl)

    picked = await downloader._search_subdl(
        "Movie", 2020, "ZH-Hans", None, "Movie.2020.2160p.BluRay.MTeam.mkv"
    )

    assert [r.filename for r in picked] == [
        "Movie.2020.2160p.BluRay.MTeam.chs.srt",
        "Movie.2020.1080p.WEBRip.PSA.chs.srt",
    ]


@pytest.mark.asyncio
async def test_download_falls_back_from_assrt_failure_to_opensubtitles(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("os-key", assrt_token="assrt-token")
    result = _result("opensubtitles")
    assrt_search = AsyncMock(side_effect=SubtitleError("ASSRT unavailable"))
    os_search = AsyncMock(return_value=[result])
    subdl_search = AsyncMock()
    save = AsyncMock(return_value=Path("/saved/video.zh.srt"))
    monkeypatch.setattr(downloader, "_search_assrt", assrt_search)
    monkeypatch.setattr(downloader, "_search_os", os_search)
    monkeypatch.setattr(downloader, "_search_subdl", subdl_search)
    monkeypatch.setattr(downloader, "_save", save)

    returned = await downloader.download("Film", 2020, Path("/media/Film"))

    assert returned == Path("/saved/video.zh.srt")
    assrt_search.assert_awaited_once()
    os_search.assert_awaited_once_with("Film", 2020, "zh-cn", None, None)
    subdl_search.assert_not_awaited()
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_falls_back_when_assrt_payload_is_not_chinese(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("os-key", assrt_token="assrt-token")
    assrt_result = _result("assrt", filename="multi-language.srt")
    os_result = _result("opensubtitles", filename="chinese.srt")
    monkeypatch.setattr(downloader, "_search_assrt", AsyncMock(return_value=[assrt_result]))
    monkeypatch.setattr(downloader, "_search_os", AsyncMock(return_value=[os_result]))
    assrt = AsyncMock()
    assrt.download.return_value = b"1\n00:00:01,000 --> 00:00:02,000\nEnglish only\n"
    opensubtitles = AsyncMock()
    opensubtitles.download.return_value = _subtitle_bytes()
    monkeypatch.setattr(downloader, "_assrt", assrt)
    monkeypatch.setattr(downloader, "_os", opensubtitles)

    returned = await downloader.download("Film", 2020, tmp_path, "video.mkv")

    assert returned == tmp_path / "video.zh.srt"
    assert returned.read_bytes() == _subtitle_bytes()
    assrt.download.assert_awaited_once_with(assrt_result, ["zh-cn"])
    opensubtitles.download.assert_awaited_once_with(os_result)


@pytest.mark.asyncio
async def test_download_falls_back_from_opensubtitles_failure_to_subdl(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("os-key", subdl_api_key="subdl-key")
    result = _result("subdl")
    os_search = AsyncMock(side_effect=SubtitleError("OpenSubtitles unavailable"))
    subdl_search = AsyncMock(return_value=[result])
    save = AsyncMock(return_value=Path("/saved/video.zh.srt"))
    monkeypatch.setattr(downloader, "_search_os", os_search)
    monkeypatch.setattr(downloader, "_search_subdl", subdl_search)
    monkeypatch.setattr(downloader, "_save", save)

    returned = await downloader.download("Film", 2020, Path("/media/Film"))

    assert returned == Path("/saved/video.zh.srt")
    os_search.assert_awaited_once()
    subdl_search.assert_awaited_once_with("Film", 2020, "ZH-Hans", None, None)
    save.assert_awaited_once()


@pytest.mark.asyncio
async def test_download_returns_none_when_all_providers_have_no_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader(
        "os-key", assrt_token="assrt-token", subdl_api_key="subdl-key"
    )
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
async def test_download_raises_when_no_provider_is_configured() -> None:
    downloader = SubtitleDownloader("")

    with pytest.raises(SubtitleError, match="未配置可用字幕源"):
        await downloader.download("Film", 2020, Path("/media/Film"))


@pytest.mark.asyncio
async def test_download_reports_provider_failure_instead_of_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("os-key")
    monkeypatch.setattr(
        downloader,
        "_search_os",
        AsyncMock(side_effect=SubtitleError("HTTP 401")),
    )

    with pytest.raises(SubtitleError, match="OpenSubtitles.*HTTP 401"):
        await downloader.download("Film", 2020, Path("/media/Film"))


@pytest.mark.asyncio
async def test_save_extracts_supported_subtitle_from_zip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    archive = BytesIO()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("readme.txt", "ignore")
        zipped.writestr("release.简体.chs.srt", _subtitle_bytes())
        zipped.writestr("release.繁体.cht.ass", _subtitle_bytes("[Script Info]"))
        zipped.writestr("special.english.ass", "English subtitle only")

    downloader = SubtitleDownloader("", subdl_api_key="subdl-key")
    _mock_subdl_download(monkeypatch, downloader, archive.getvalue())

    returned = await downloader._save(
        _result(language="英 简 繁 法 西 日 韩", filename="archive.zip"),
        tmp_path,
        "video.mkv",
    )

    assert returned == tmp_path / "video.zh.srt"
    assert returned.read_bytes() == _subtitle_bytes()


@pytest.mark.asyncio
async def test_save_rejects_mislabeled_english_subtitle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloader = SubtitleDownloader("", subdl_api_key="subdl-key")
    _mock_subdl_download(
        monkeypatch,
        downloader,
        b"1\n00:00:01,000 --> 00:00:02,000\nThis subtitle is English only.\n",
    )

    with pytest.raises(SubtitleError, match="实际不含中文"):
        await downloader._save(_result(language="zh-cn"), tmp_path, "video.mkv")

    assert not (tmp_path / "video.zh.srt").exists()


@pytest.mark.asyncio
async def test_save_rejects_japanese_subtitle_in_multilingual_archive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    japanese = "これは日本語の字幕です。映画の内容を説明するための文章が続きます。" * 4
    archive = BytesIO()
    with ZipFile(archive, "w") as zipped:
        zipped.writestr("movie.jpn.srt", japanese)
        zipped.writestr("movie.eng.srt", "English subtitle only")
    downloader = SubtitleDownloader("", subdl_api_key="subdl-key")
    _mock_subdl_download(monkeypatch, downloader, archive.getvalue())

    with pytest.raises(SubtitleError, match="没有检测到中文正文"):
        await downloader._save(_result(filename="archive.zip"), tmp_path, "video.mkv")


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
