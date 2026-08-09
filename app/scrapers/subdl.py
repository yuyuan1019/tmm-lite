"""SubDL.com API scraper used as a credentialed subtitle fallback."""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import urljoin, urlparse

import httpx

from app.exceptions import TmmError
from app.scrapers.opensubtitles import SubtitleResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.subdl.com/api/v1"
_DOWNLOAD_BASE_URL = "https://dl.subdl.com"


class SubDLError(TmmError):
    """Error returned by the SubDL API or download host."""


class SubDLScraper:
    """Async client for the SubDL API."""

    def __init__(self, api_key: str, proxy: str = "") -> None:
        self._api_key = api_key
        self._proxy = proxy
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
            proxy=proxy or None,
            headers={"Accept": "application/json"},
        )
        self._last_request_time = 0.0
        self._min_interval = 2.0

    async def search(
        self,
        title: str,
        year: int | None = None,
        language: str = "ZH",
        imdb_id: str | None = None,
    ) -> list[SubtitleResult]:
        """Search SubDL for subtitles."""
        await self._rate_limit()
        params: dict[str, str] = {
            "api_key": self._api_key,
            "languages": language,
            "unpack": "1",
            "subs_per_page": "30",
            "client": "other",
        }
        if imdb_id:
            params["imdb_id"] = imdb_id
        else:
            params["film_name"] = title
            if year is not None:
                params["year"] = str(year)

        try:
            resp = await self._client.get("/subtitles", params=params)
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise SubDLError(f"SubDL 搜索失败: HTTP {exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise SubDLError(f"SubDL 搜索失败: {type(exc).__name__}") from exc

        if data.get("status") is False:
            raise SubDLError(f"SubDL 搜索失败: {data.get('error') or 'unknown error'}")

        results: list[SubtitleResult] = []
        subtitles = data.get("subtitles") or []
        if not isinstance(subtitles, list):
            raise SubDLError("SubDL 搜索失败: 响应中的 subtitles 不是列表")
        for item in subtitles:
            if not isinstance(item, dict):
                continue
            unpack_files = item.get("unpack_files") or []
            if isinstance(unpack_files, list) and unpack_files:
                for unpacked in unpack_files:
                    if isinstance(unpacked, dict):
                        parsed = self._parse_result(unpacked, language, title)
                        if parsed is not None:
                            results.append(parsed)
                continue
            parsed = self._parse_result(item, language, title)
            if parsed is not None:
                results.append(parsed)

        return results[:10]

    async def download(self, url: str) -> bytes:
        """Download the subtitle file from *url*."""
        await self._rate_limit()
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname != "dl.subdl.com":
            raise SubDLError("SubDL 返回了不受信任的下载地址")
        try:
            resp = await self._client.get(url)
            resp.raise_for_status()
            return resp.content
        except httpx.HTTPStatusError as exc:
            raise SubDLError(f"SubDL 下载失败: HTTP {exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise SubDLError(f"SubDL 下载失败: {type(exc).__name__}") from exc

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rate_limit(self) -> None:
        now = asyncio.get_event_loop().time()
        wait = self._last_request_time + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_time = asyncio.get_event_loop().time()

    @staticmethod
    def _parse_result(
        item: dict[str, object],
        default_language: str,
        default_title: str,
    ) -> SubtitleResult | None:
        raw_url = item.get("url") or item.get("download_url")
        if not isinstance(raw_url, str) or not raw_url:
            return None
        download_url = urljoin(f"{_DOWNLOAD_BASE_URL}/", raw_url)
        parsed = urlparse(download_url)
        if parsed.scheme != "https" or parsed.hostname != "dl.subdl.com":
            return None
        language = item.get("language") or default_language
        filename = (
            item.get("name")
            or item.get("filename")
            or item.get("release_name")
            or default_title
        )
        return SubtitleResult(
            provider="subdl",
            language=str(language),
            filename=str(filename),
            download_url=download_url,
            score=0.0,
            hearing_impaired=bool(item.get("hi", False)),
        )
