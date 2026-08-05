"""OpenSubtitles.com API scraper for subtitle download.

Uses the REST API v1.  Requires a free API key from opensubtitles.com.
Rate limit: 20 requests/minute on the free tier.

ISO 639-2 language codes used: chi (Chinese), zho (Chinese), eng (English), etc.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.exceptions import TmmError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.opensubtitles.com/api/v1"


@dataclass
class SubtitleResult:
    """A subtitle candidate from a provider."""

    provider: str
    language: str
    filename: str
    download_url: str
    score: float = 0.0
    hearing_impaired: bool = False


class OpenSubtitlesError(TmmError):
    """Error from the OpenSubtitles API."""


class OpenSubtitlesScraper:
    """Async client for the OpenSubtitles.com REST API."""

    def __init__(self, api_key: str, user_agent: str = "TMM-Lite") -> None:
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Api-Key": api_key,
                "User-Agent": user_agent or "TMM-Lite",
            },
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
        )
        self._last_request_time = 0.0
        self._min_interval = 3.0  # 20 req/min = 3s between

    async def search(
        self,
        title: str,
        year: int | None = None,
        language: str = "chi",
        imdb_id: str | None = None,
    ) -> list[SubtitleResult]:
        """Search for subtitles. Returns up to 10 results sorted by score.

        Args:
            title: Film or TV show title.
            year: Release year (narrows results).
            language: ISO 639-2 language code (comma-separated for multiple).
            imdb_id: IMDb ID (e.g. ``tt0816692``) for exact match.
        """
        await self._rate_limit()
        params: dict[str, str] = {
            "query": title,
            "languages": language,
        }
        if year is not None:
            params["year"] = str(year)
        if imdb_id:
            # REST API expects a bare numeric IMDb id (no "tt" prefix).
            params["imdb_id"] = imdb_id.removeprefix("tt")

        try:
            resp = await self._client.get("/subtitles", params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 (API failures must not crash)
            logger.warning("OpenSubtitles search failed: %s", exc)
            return []

        results: list[SubtitleResult] = []
        for item in data.get("data", []):
            attrs = item.get("attributes", {})
            files = attrs.get("files", [])
            if not files:
                continue
            # Prefer the first file
            fid = files[0].get("file_id")
            if not fid:
                continue
            results.append(SubtitleResult(
                provider="opensubtitles",
                language=attrs.get("language", language),
                filename=attrs.get("release", title),
                download_url=f"/download/{fid}",
                score=float(attrs.get("ratings", 0) or 0),
                hearing_impaired=bool(attrs.get("hearing_impaired", False)),
            ))

        # Sort by score descending
        results.sort(key=lambda r: r.score, reverse=True)
        return results[:10]

    async def download(self, result: SubtitleResult) -> bytes:
        """Download the subtitle file content."""
        await self._rate_limit()
        try:
            resp = await self._client.post(
                result.download_url,
                headers={"Accept": "application/json"},
            )
            resp.raise_for_status()
            data = resp.json()
            link = data.get("link") or data.get("remaining_link")
            if not link:
                raise OpenSubtitlesError("No download link in response")

            # Fetch the actual file
            resp2 = await self._client.get(link)
            resp2.raise_for_status()
            return resp2.content
        except Exception as exc:
            raise OpenSubtitlesError(f"Subtitle download failed: {exc}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rate_limit(self) -> None:
        now = asyncio.get_event_loop().time()
        wait = self._last_request_time + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_time = asyncio.get_event_loop().time()
