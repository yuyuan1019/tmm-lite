"""Unified subtitle downloader — tries OpenSubtitles, falls back to SubDL.

Best-effort: failures are logged but never propagate to the caller.
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.connection import Connection
from app.scrapers.assrt import AssrtScraper
from app.scrapers.opensubtitles import OpenSubtitlesScraper, SubtitleResult
from app.scrapers.subdl import SubDLScraper

logger = logging.getLogger(__name__)

# ISO 639-2 codes for common subtitle languages
_LANG_MAP = {
    "zh": "chi",       # Simplified Chinese (also "zho")
    "zh-tw": "chi",    # Traditional Chinese → closest is chi
    "en": "eng",
    "ja": "jpn",
    "ko": "kor",
    "fr": "fre",
    "de": "ger",
    "es": "spa",
    "pt": "por",
    "ru": "rus",
    "ar": "ara",
    "hi": "hin",
    "th": "tha",
    "vi": "vie",
}


class SubtitleDownloader:
    """Downloads subtitles using OpenSubtitles (primary) + SubDL (fallback).

    Args:
        opensubtitles_api_key: API key for opensubtitles.com (may be empty).
        preferred_languages: Comma-separated ISO 639-2 codes, default ``chi,zho,zh``.
    """

    def __init__(
        self,
        opensubtitles_api_key: str,
        preferred_languages: str = "chi,zho,zh",
        opensubtitles_user_agent: str = "TMM-Lite",
        assrt_token: str = "",
    ) -> None:
        self._os_key = opensubtitles_api_key
        self._os_user_agent = opensubtitles_user_agent or "TMM-Lite"
        self._assrt_token = assrt_token
        self._languages = [l.strip() for l in preferred_languages.split(",") if l.strip()]
        if not self._languages:
            self._languages = ["chi"]

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
        lang_str = ",".join(self._languages[:3])  # max 3 languages per query

        # Try ASSRT first (Chinese-focused — best hit rate for zh subtitles)
        if self._assrt_token:
            try:
                result = await self._search_assrt(title, year, lang_str)
                if result is not None:
                    return await self._save(result, media_folder, video_filename, connection)
                logger.info("ASSRT: 无结果 %s (%s)", title, year)
            except Exception:
                logger.warning("ASSRT failed, trying OpenSubtitles", exc_info=True)

        # Try OpenSubtitles
        if self._os_key:
            try:
                result = await self._search_os(title, year, lang_str, imdb_id)
                if result is not None:
                    return await self._save(result, media_folder, video_filename, connection)
                logger.info("OpenSubtitles: 无结果 %s (%s, imdb=%s)", title, year, imdb_id)
            except Exception:
                logger.warning("OpenSubtitles failed, trying SubDL", exc_info=True)

        # Fallback to SubDL
        try:
            result = await self._search_subdl(title, year, lang_str)
            if result is not None:
                return await self._save(result, media_folder, video_filename, connection)
            logger.info("SubDL: 无结果 %s (%s)", title, year)
        except Exception:
            logger.warning("SubDL failed, no subtitles downloaded", exc_info=True)

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
            self._os = OpenSubtitlesScraper(self._os_key, self._os_user_agent)
        results = await self._os.search(title, year, languages, imdb_id)
        # Prefer non-HI (hearing impaired) results
        for r in results:
            if not r.hearing_impaired:
                return r
        return results[0] if results else None

    async def _search_assrt(
        self, title: str, year: int | None, languages: str,
    ) -> SubtitleResult | None:
        if self._assrt is None:
            self._assrt = AssrtScraper(self._assrt_token)
        results = await self._assrt.search(title, year, languages)
        return results[0] if results else None

    async def _search_subdl(
        self, title: str, year: int | None, languages: str,
    ) -> SubtitleResult | None:
        if self._subdl is None:
            self._subdl = SubDLScraper()
        results = await self._subdl.search(title, year, languages)
        return results[0] if results else None

    async def _save(
        self,
        result: SubtitleResult,
        folder: Path,
        video_filename: str | None = None,
        connection: Connection | None = None,
    ) -> Path:
        """Download and save the subtitle file."""
        if result.provider == "opensubtitles":
            if self._os is None:
                self._os = OpenSubtitlesScraper(self._os_key, self._os_user_agent)
            data = await self._os.download(result)
        elif result.provider == "assrt":
            if self._assrt is None:
                self._assrt = AssrtScraper(self._assrt_token)
            data = await self._assrt.download(result)
        else:  # subdl
            if self._subdl is None:
                self._subdl = SubDLScraper()
            data = await self._subdl.download(result.download_url)

        # Determine filename: prefer matching the video file stem
        if video_filename:
            video_path = Path(video_filename)
            stem = video_path.stem
            dest_folder = folder / video_path.parent
        else:
            stem = "subtitles"
            dest_folder = folder

        # Extract language code for suffix
        lang_code = result.language.split("-")[0].lower() if result.language else "zh"
        if lang_code in ("chi", "zho", "zh"):
            suffix = "zh"
        elif lang_code in ("eng", "en"):
            suffix = "en"
        else:
            suffix = lang_code

        dest = dest_folder / f"{stem}.{suffix}.srt"
        rel_path = dest.relative_to(folder).as_posix() if connection is not None else None

        if connection is not None and rel_path is not None:
            await connection.write_bytes(rel_path, data)
        else:
            dest_folder.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        logger.info("Subtitle saved: %s", dest)
        return dest
