"""ASSRT (伪射手网, assrt.net) subtitle scraper — Chinese-focused provider.

Uses the free REST API (register at https://assrt.net for a token; view it in
the user panel). Responses are JSON. The API has no language filter, but ASSRT
is a Chinese subtitle site so results are predominantly Chinese.

Rate limit: 20 requests/minute shared per token and per IP.
"""

from __future__ import annotations

import asyncio
import logging

import httpx

from app.exceptions import TmmError
from app.scrapers.opensubtitles import SubtitleResult

logger = logging.getLogger(__name__)

_BASE_URL = "https://api.assrt.net/v1"


class AssrtError(TmmError):
    """Error from the ASSRT API."""


class AssrtScraper:
    """Async client for the ASSRT REST API."""

    def __init__(self, token: str) -> None:
        self._token = token
        self._client = httpx.AsyncClient(
            base_url=_BASE_URL,
            timeout=httpx.Timeout(15.0),
            follow_redirects=True,
        )
        self._last_request_time = 0.0
        # ASSRT free tier is often 5 req/min (shared per token + IP); 12s
        # between API calls stays safely under that. A subtitle = 2 API calls
        # (search + detail); the file download hits file*.assrt.net (signed
        # URL, not token-authed) and is not counted against this quota.
        self._min_interval = 12.0

    async def search(
        self,
        title: str,
        year: int | None = None,
        language: str = "chi",
    ) -> list[SubtitleResult]:
        """Search ASSRT for subtitles by keyword.

        ``year`` and ``language`` are accepted for interface symmetry but not
        sent: the ASSRT API has no such filters. Returns up to 10 candidates
        sorted by vote score.
        """
        await self._rate_limit()
        params: dict[str, str] = {"token": self._token, "q": title, "cnt": "10"}
        try:
            resp = await self._client.get("/sub/search", params=params)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:  # noqa: BLE001 (API failures must not crash)
            logger.warning("ASSRT search failed: %s", exc)
            return []

        if data.get("status") != 0:
            logger.info("ASSRT search error status=%s", data.get("status"))
            return []

        subs = (data.get("sub") or {}).get("subs") or []
        results: list[SubtitleResult] = []
        for s in subs:
            sid = s.get("id")
            if sid is None:
                continue
            lang_block = s.get("lang") or {}
            lang_desc = lang_block.get("desc") or language
            results.append(
                SubtitleResult(
                    provider="assrt",
                    language=str(lang_desc),
                    filename=str(s.get("native_name") or s.get("videoname") or title),
                    # Store the sub id; the detail endpoint is fetched at download.
                    download_url=str(sid),
                    score=float(s.get("vote_score", 0) or 0),
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return results[:10]

    async def download(self, result: SubtitleResult) -> bytes:
        """Fetch the subtitle bytes for a search result.

        Resolves the sub id via ``/sub/detail`` and downloads the first file in
        its ``filelist`` (a signed direct link to a raw ``.srt``/``.ass`` — no
        archive extraction needed).
        """
        await self._rate_limit()
        try:
            resp = await self._client.get(
                "/sub/detail", params={"token": self._token, "id": result.download_url},
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            raise AssrtError(f"ASSRT detail failed: {exc}") from exc

        subs = (data.get("sub") or {}).get("subs") or []
        filelist = subs[0].get("filelist") or [] if subs else []
        for f in filelist:
            url = f.get("url")
            if not url:
                continue
            try:
                r = await self._client.get(str(url))
                r.raise_for_status()
                return r.content
            except Exception as exc:  # noqa: BLE001
                logger.debug("ASSRT file download failed (%s): %s", f.get("f"), exc)
                continue

        raise AssrtError("ASSRT: no downloadable file in filelist")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rate_limit(self) -> None:
        now = asyncio.get_event_loop().time()
        wait = self._last_request_time + self._min_interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request_time = asyncio.get_event_loop().time()
