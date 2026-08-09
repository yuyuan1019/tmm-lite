"""Unified subtitle downloader with provider fallback and explicit failures."""

from __future__ import annotations

import logging
import posixpath
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, is_zipfile

from app.connection import Connection
from app.exceptions import SubtitleError, TmmError
from app.scrapers.assrt import AssrtScraper
from app.scrapers.opensubtitles import DEFAULT_USER_AGENT, OpenSubtitlesScraper, SubtitleResult
from app.scrapers.subdl import SubDLScraper

logger = logging.getLogger(__name__)

_SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub"})
_MAX_ARCHIVE_ENTRIES = 100
_MAX_SUBTITLE_BYTES = 10 * 1024 * 1024

# Legacy ISO 639-2 values are accepted and translated for modern provider APIs.
_ISO_639_2_TO_1 = {
    "eng": "en",
    "jpn": "ja",
    "kor": "ko",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "spa": "es",
    "por": "pt-pt",
    "rus": "ru",
    "ara": "ar",
    "hin": "hi",
    "tha": "th",
    "vie": "vi",
}


def _subtitle_extension(filename: str) -> str:
    """Return a supported subtitle extension, defaulting to ``.srt``."""
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    return suffix if suffix in _SUBTITLE_EXTENSIONS else ".srt"


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _opensubtitles_languages(languages: list[str]) -> str:
    """Translate user/legacy language values to OpenSubtitles language codes."""
    normalized: list[str] = []
    for raw in languages:
        code = raw.strip().lower().replace("_", "-")
        if code in {"chi", "zho", "zh"}:
            normalized.extend(("zh-cn", "zh-tw", "ze"))
        elif code in {"zh-cn", "zh-tw", "ze"}:
            normalized.append(code)
        else:
            normalized.append(_ISO_639_2_TO_1.get(code, code))
    return ",".join(_unique(normalized)[:3])


def _subdl_languages(languages: list[str]) -> str:
    """Translate user/legacy language values to SubDL's upper-case codes."""
    normalized: list[str] = []
    for raw in languages:
        code = raw.strip().lower().replace("_", "-")
        if code in {"chi", "zho", "zh", "zh-cn", "zh-tw", "ze"}:
            normalized.append("ZH")
        else:
            normalized.append(_ISO_639_2_TO_1.get(code, code).split("-")[0].upper())
    return ",".join(_unique(normalized)[:3])


def _subtitle_suffix(language: str) -> str:
    code = language.strip().lower().replace("_", "-")
    if (
        code in {"chi", "zho", "zh", "zh-cn", "zh-tw", "ze"}
        or any(marker in code for marker in ("中文", "简体", "繁体", "中英", "双语"))
    ):
        return "zh"
    if code in {"eng", "en"}:
        return "en"
    return _ISO_639_2_TO_1.get(code, code).split("-")[0] or "zh"


def _unpack_subtitle(data: bytes, filename: str) -> tuple[bytes, str]:
    """Return a usable subtitle payload, extracting safe ZIP members in memory."""
    stream = BytesIO(data)
    if not is_zipfile(stream):
        return data, filename
    stream.seek(0)
    try:
        with ZipFile(stream) as archive:
            members = [
                info
                for info in archive.infolist()[:_MAX_ARCHIVE_ENTRIES]
                if not info.is_dir()
                and PurePosixPath(info.filename.replace("\\", "/")).suffix.lower()
                in _SUBTITLE_EXTENSIONS
                and 0 < info.file_size <= _MAX_SUBTITLE_BYTES
                and not (info.flag_bits & 0x1)
            ]
            if not members:
                raise SubtitleError("字幕压缩包中没有可用的字幕文件")
            priority = {".ass": 0, ".ssa": 1, ".srt": 2, ".vtt": 3, ".sub": 4}
            member = min(
                members,
                key=lambda info: (
                    priority.get(
                        PurePosixPath(info.filename.replace("\\", "/")).suffix.lower(),
                        99,
                    ),
                    info.filename.lower(),
                ),
            )
            payload = archive.read(member)
            if not payload:
                raise SubtitleError("字幕压缩包中的字幕文件为空")
            return payload, member.filename
    except (BadZipFile, RuntimeError) as exc:
        raise SubtitleError("字幕压缩包损坏或无法读取") from exc


def _normalized_posix_path(path: str | Path | PurePosixPath) -> PurePosixPath:
    """Normalize local or remote path syntax without resolving the filesystem."""
    return PurePosixPath(posixpath.normpath(str(path).replace("\\", "/")))


class SubtitleDownloader:
    """Download subtitles through ASSRT, OpenSubtitles, then SubDL.

    Args:
        opensubtitles_api_key: API key for opensubtitles.com (may be empty).
        preferred_languages: Comma-separated language codes, default ``zh-cn``.
    """

    def __init__(
        self,
        opensubtitles_api_key: str,
        preferred_languages: str = "zh-cn",
        assrt_token: str = "",
        subdl_api_key: str = "",
        proxy: str = "",
    ) -> None:
        self._os_key = opensubtitles_api_key
        self._assrt_token = assrt_token
        self._subdl_api_key = subdl_api_key
        self._proxy = proxy
        self._languages = [l.strip() for l in preferred_languages.split(",") if l.strip()]
        if not self._languages:
            self._languages = ["zh-cn"]

        # Lazy-initialised
        self._assrt: AssrtScraper | None = None
        self._os: OpenSubtitlesScraper | None = None
        self._subdl: SubDLScraper | None = None

    async def download(
        self,
        title: str,
        year: int | None,
        media_folder: Path,
        video_filename: str | None = None,
        imdb_id: str | None = None,
        connection: Connection | None = None,
    ) -> Path | None:
        """Download the best-matching subtitle to *media_folder*.

        Returns the local path to the downloaded file, or ``None``.
        When *connection* is provided, the subtitle is written through it
        instead of to the local filesystem.
        """
        assrt_languages = ",".join(self._languages[:3])
        opensubtitles_languages = _opensubtitles_languages(self._languages)
        subdl_languages = _subdl_languages(self._languages)
        attempted = 0
        failures: list[str] = []

        # Try ASSRT first (Chinese-focused — best hit rate for zh subtitles)
        if self._assrt_token:
            attempted += 1
            try:
                result = await self._search_assrt(title, year, assrt_languages)
                if result is not None:
                    return await self._save(result, media_folder, video_filename, connection)
                logger.info("ASSRT: 无结果 %s (%s)", title, year)
            except TmmError as exc:
                failures.append(f"ASSRT: {exc}")
                logger.warning("ASSRT failed, trying OpenSubtitles: %s", exc)

        # Try OpenSubtitles
        if self._os_key:
            attempted += 1
            try:
                result = await self._search_os(
                    title, year, opensubtitles_languages, imdb_id
                )
                if result is not None:
                    return await self._save(result, media_folder, video_filename, connection)
                logger.info("OpenSubtitles: 无结果 %s (%s, imdb=%s)", title, year, imdb_id)
            except TmmError as exc:
                failures.append(f"OpenSubtitles: {exc}")
                logger.warning("OpenSubtitles failed, trying SubDL: %s", exc)

        # Fallback to SubDL
        if self._subdl_api_key:
            attempted += 1
            try:
                result = await self._search_subdl(title, year, subdl_languages, imdb_id)
                if result is not None:
                    return await self._save(result, media_folder, video_filename, connection)
                logger.info("SubDL: 无结果 %s (%s)", title, year)
            except TmmError as exc:
                failures.append(f"SubDL: {exc}")
                logger.warning("SubDL failed, no subtitles downloaded: %s", exc)

        if attempted == 0:
            raise SubtitleError("未配置可用字幕源，请填写 ASSRT、OpenSubtitles 或 SubDL 密钥")
        if failures:
            raise SubtitleError("字幕源请求失败：" + "；".join(failures))

        logger.info("未下载到字幕: %s (%s, imdb=%s)", title, year, imdb_id)
        return None

    async def aclose(self) -> None:
        if self._assrt is not None:
            await self._assrt.aclose()
            self._assrt = None
        if self._os is not None:
            await self._os.aclose()
            self._os = None
        if self._subdl is not None:
            await self._subdl.aclose()
            self._subdl = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _search_os(
        self, title: str, year: int | None, languages: str, imdb_id: str | None,
    ) -> SubtitleResult | None:
        if self._os is None:
            self._os = OpenSubtitlesScraper(
                self._os_key, DEFAULT_USER_AGENT, proxy=self._proxy
            )
        results = await self._os.search(title, year, languages, imdb_id)
        if not results and imdb_id:
            results = await self._os.search(title, year, languages, None)
        # Prefer non-HI (hearing impaired) results
        for r in results:
            if not r.hearing_impaired:
                return r
        return results[0] if results else None

    async def _search_assrt(
        self, title: str, year: int | None, languages: str,
    ) -> SubtitleResult | None:
        if self._assrt is None:
            self._assrt = AssrtScraper(self._assrt_token, proxy=self._proxy)
        results = await self._assrt.search(title, year, languages)
        return results[0] if results else None

    async def _search_subdl(
        self,
        title: str,
        year: int | None,
        languages: str,
        imdb_id: str | None,
    ) -> SubtitleResult | None:
        if self._subdl is None:
            self._subdl = SubDLScraper(self._subdl_api_key, proxy=self._proxy)
        results = await self._subdl.search(title, year, languages, imdb_id)
        if not results and imdb_id:
            results = await self._subdl.search(title, year, languages, None)
        return results[0] if results else None

    async def _save(
        self,
        result: SubtitleResult,
        folder: Path,
        video_filename: str | None = None,
        connection: Connection | None = None,
    ) -> Path:
        """Download and save the subtitle file."""
        folder_path = _normalized_posix_path(folder)

        # Prefer the video stem and keep a nested video's relative directory.
        if video_filename:
            video_path = _normalized_posix_path(video_filename)
            if video_path.is_absolute() or ".." in video_path.parts:
                raise ValueError("video_filename must be relative to the media folder")
            stem = video_path.stem
            dest_folder = _normalized_posix_path(folder_path / video_path.parent)
        else:
            stem = "subtitles"
            dest_folder = folder_path

        # Reject an out-of-root destination before making a provider download request.
        root_path: PurePosixPath | None = None
        if connection is not None:
            root_path = _normalized_posix_path(connection.root)
            try:
                dest_folder.relative_to(root_path)
            except ValueError as exc:
                raise ValueError(
                    f"Subtitle destination {dest_folder} is outside connection root {root_path}",
                ) from exc

        if result.provider == "opensubtitles":
            if self._os is None:
                self._os = OpenSubtitlesScraper(
                    self._os_key, DEFAULT_USER_AGENT, proxy=self._proxy
                )
            data = await self._os.download(result)
        elif result.provider == "assrt":
            if self._assrt is None:
                self._assrt = AssrtScraper(self._assrt_token, proxy=self._proxy)
            data = await self._assrt.download(result)
        elif result.provider == "subdl":
            if self._subdl is None:
                self._subdl = SubDLScraper(self._subdl_api_key, proxy=self._proxy)
            data = await self._subdl.download(result.download_url)
        else:
            raise SubtitleError(f"不支持的字幕源: {result.provider}")

        if not data:
            raise SubtitleError(f"{result.provider} 返回了空字幕文件")
        data, payload_filename = _unpack_subtitle(data, result.filename)
        if len(data) > _MAX_SUBTITLE_BYTES:
            raise SubtitleError("字幕文件过大，已拒绝保存")

        suffix = _subtitle_suffix(result.language or "zh")
        extension = _subtitle_extension(payload_filename)
        dest_path = _normalized_posix_path(dest_folder / f"{stem}.{suffix}{extension}")
        dest = Path(dest_path.as_posix())

        # Connections always consume paths relative to their configured root.
        rel_path: str | None = None
        if root_path is not None:
            rel_path = dest_path.relative_to(root_path).as_posix()

        if connection is not None and rel_path is not None:
            await connection.write_bytes(rel_path, data)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        logger.info("Subtitle saved: %s", dest)
        return dest
