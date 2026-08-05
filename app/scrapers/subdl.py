"""SubDL.com API scraper — free subtitle provider, no API key required.

Used as a fallback when OpenSubtitles has no results.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.scrapers.opensubtitles import SubtitleResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://subdl.com/api/v1"


class SubDLScraper:
    """Async client for the SubDL API (no auth required)."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0), follow_redirects=True)
        self._last_request_time = 0.0
        self._min_interval = 2.0

    async def search(
        self,
        title: str,
        year: int | None = None,
        language: str = "chi",
    ) -> list[SubtitleResult]:
        """Search SubDL for subtitles."""
        await self._rate_limit()
        params: dict[str, str] = {
            "film_name": title,
            "languages": language,
        }
        if year is not None:
            params["year"] = str(year)

        try:
            resp = await self._client.get(
                f"{_BASE_URL}/subtitles", params=params,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 (API failures must not crash)
            logger.warning("SubDL search failed: %s", exc)
            return []

        results: list[SubtitleResult] = []
        for item in data.get("results", data.get("subtitles", [])):
            if isinstance(item, dict):
                download_url = item.get("url") or item.get("download_url") or ""
                lang = item.get("language", language)
                filename = item.get("name") or item.get("filename", title)
                results.append(SubtitleResult(
                    provider="subdl",
                    language=str(lang),
                    filename=str(filename),
                    download_url=str(download_url),
                    score=0.0,
                ))

        return results[:10]

    async def download(self, url: str) -> bytes:
        """Download the subtitle file from *url*."""
        await self._rate_limit()
        resp = await self._client.get(url)
        resp.raise_for_status()
        return resp.content

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rate_limit(self) -> None:
        now = asyncio.get_event_loop().time()
        wait = self._last_request_time + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_time = asyncio.get_event_loop().time()
