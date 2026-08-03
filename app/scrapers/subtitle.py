"""Unified subtitle downloader — tries OpenSubtitles, falls back to SubDL.

Best-effort: failures are logged but never propagate to the caller.
"""

from __future__ import annotations

import logging
from pathlib import Path

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
    ) -> None:
        self._os_key = opensubtitles_api_key
        self._languages = [l.strip() for l in preferred_languages.split(",") if l.strip()]
        if not self._languages:
            self._languages = ["chi"]

        # Lazy-initialised
        self._os: OpenSubtitlesScraper | None = None
        self._subdl: SubDLScraper | None = None

    async def download(
        self,
        title: str,
        year: int | None,
        media_folder: Path,
        video_filename: str | None = None,
        imdb_id: str | None = None,
    ) -> Path | None:
        """Download the best-matching subtitle to *media_folder*.

        Returns the path to the downloaded file, or ``None``.
        """
        lang_str = ",".join(self._languages[:3])  # max 3 languages per query

        # Try OpenSubtitles first
        if self._os_key:
            try:
                result = await self._search_os(title, year, lang_str, imdb_id)
                if result is not None:
                    return await self._save(result, media_folder, video_filename)
            except Exception:
                logger.warning("OpenSubtitles failed, trying SubDL", exc_info=True)

        # Fallback to SubDL
        try:
            result = await self._search_subdl(title, year, lang_str)
            if result is not None:
                return await self._save(result, media_folder, video_filename)
        except Exception:
            logger.warning("SubDL failed, no subtitles downloaded", exc_info=True)

        return None

    async def aclose(self) -> None:
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
            self._os = OpenSubtitlesScraper(self._os_key)
        results = await self._os.search(title, year, languages, imdb_id)
        # Prefer non-HI (hearing impaired) results
        for r in results:
            if not r.hearing_impaired:
                return r
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
    ) -> Path:
        """Download and save the subtitle file."""
        if result.provider == "opensubtitles" and self._os is not None:
            data = await self._os.download(result)
        elif self._subdl is not None:
            data = await self._subdl.download(result.download_url)
        else:
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

        dest_folder.mkdir(parents=True, exist_ok=True)
        dest = dest_folder / f"{stem}.{suffix}.srt"
        dest.write_bytes(data)
        logger.info("Subtitle saved: %s", dest)
        return dest
