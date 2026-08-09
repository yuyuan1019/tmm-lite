"""OpenSubtitles.com API scraper for subtitle download.

Uses the REST API v1. Requires a free API key from opensubtitles.com.
Rate limit: 20 requests/minute on the free tier.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

import httpx

from app.exceptions import TmmError

logger = logging.getLogger(__name__)

BASE_URL = "https://api.opensubtitles.com/api/v1"
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


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

    def __init__(
        self,
        api_key: str,
        user_agent: str = DEFAULT_USER_AGENT,
        proxy: str = "",
    ) -> None:
        self._proxy = proxy
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            headers={
                "Api-Key": api_key,
                "User-Agent": user_agent or DEFAULT_USER_AGENT,
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            proxy=proxy or None,
        )
        # Do not leak the API key to the temporary cross-origin download URL.
        self._download_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0),
            follow_redirects=True,
            proxy=proxy or None,
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
        params: dict[str, str] = {"languages": language}
        if imdb_id:
            # REST API expects a bare numeric IMDb id (no "tt" prefix).
            params["imdb_id"] = imdb_id.removeprefix("tt")
        else:
            params["query"] = title
            if year is not None:
                params["year"] = str(year)

        try:
            resp = await self._client.get("/subtitles", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise OpenSubtitlesError(
                f"OpenSubtitles 搜索失败: HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise OpenSubtitlesError(
                f"OpenSubtitles 搜索失败: {type(exc).__name__}"
            ) from exc

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
                filename=files[0].get("file_name") or attrs.get("release") or title,
                # The download endpoint expects file_id in a JSON request body.
                download_url=str(fid),
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
                "/download",
                json={"file_id": int(result.download_url)},
            )
            resp.raise_for_status()
            data = resp.json()
            link = data.get("link") or data.get("remaining_link")
            if not link:
                raise OpenSubtitlesError("No download link in response")

            # Fetch the actual file
            resp2 = await self._download_client.get(link)
            resp2.raise_for_status()
            return resp2.content
        except httpx.HTTPStatusError as exc:
            raise OpenSubtitlesError(
                f"OpenSubtitles 下载失败: HTTP {exc.response.status_code}"
            ) from exc
        except (httpx.HTTPError, TypeError, ValueError) as exc:
            raise OpenSubtitlesError(
                f"OpenSubtitles 下载失败: {type(exc).__name__}"
            ) from exc

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._download_client.aclose()

    async def _rate_limit(self) -> None:
        now = asyncio.get_event_loop().time()
        wait = self._last_request_time + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_time = asyncio.get_event_loop().time()
