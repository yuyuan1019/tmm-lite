"""Douban supplementary scraper (M6).

Unofficial scraping of movie.douban.com to supplement Chinese overviews and
ratings.  Uses a two-step process:

1. Suggest API (JSON) for candidate search + year validation.
2. Subject HTML page parse for rating and summary.

All exceptions are caught internally — failures **never** propagate to the
caller.  Rate-limited via :class:`RateLimiter`.

See implementation spec §8.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

import httpx
from lxml import etree

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (§8.1)
# ---------------------------------------------------------------------------

_DOUBAN_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
_DOUBAN_REFERER = "https://movie.douban.com/"

# XPath selectors (module-level constants for easy maintenance)
_XPATH_RATING = (
    "//strong[contains(concat(' ', normalize-space(@class), ' '), ' rating_num ')]"
)
_XPATH_SUMMARY = "//span[@property='v:summary']"

# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DoubanSupplement:
    """Supplementary metadata from Douban."""

    overview: str | None
    rating: float | None


# ---------------------------------------------------------------------------
# Rate limiter (§8.2)
# ---------------------------------------------------------------------------


class RateLimiter:
    """Async rate limiter enforcing a minimum interval between calls."""

    def __init__(self, min_interval: float) -> None:
        if not math.isfinite(min_interval) or min_interval < 0.5:
            raise ValueError(f"豆瓣请求间隔必须 >= 0.5 秒: {min_interval}")
        self._min = min_interval
        self._last: float = 0.0  # monotonic
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Block until the minimum interval since the last call has elapsed."""
        async with self._lock:
            now = time.monotonic()
            delta = self._last + self._min - now
            if delta > 0:
                await asyncio.sleep(delta)
            self._last = time.monotonic()


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------


class DoubanScraper:
    """Async Douban scraper for supplementary Chinese metadata.

    Args:
        delay_seconds: Minimum seconds between HTTP requests (≥ 0.5).
    """

    def __init__(self, delay_seconds: float) -> None:
        self._limiter = RateLimiter(delay_seconds)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0),
            headers={"User-Agent": _DOUBAN_UA, "Referer": _DOUBAN_REFERER},
        )

    async def fetch_supplement(
        self, title: str, expected_year: int | None,
    ) -> DoubanSupplement | None:
        """Fetch Douban supplementary data for *title*.

        Returns ``None`` on any failure (network, parse error, no results, or
        year mismatch across the first 3 candidates).  Never raises.
        """
        try:
            return await self._fetch_impl(title, expected_year)
        except Exception as exc:  # noqa: BLE001 (intentional: never propagate)
            logger.warning("豆瓣抓取失败(%s): %s", title, exc)
            return None

    async def aclose(self) -> None:
        """Close the underlying HTTP client."""
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _fetch_impl(
        self, title: str, expected_year: int | None,
    ) -> DoubanSupplement | None:
        # Step 1: suggest API
        await self._limiter.wait()
        subject_id = await self._find_subject_id(title, expected_year)
        if subject_id is None:
            return None

        # Step 2: detail page
        await self._limiter.wait()
        return await self._parse_subject(subject_id)

    async def _find_subject_id(
        self, title: str, expected_year: int | None,
    ) -> str | None:
        """Search the suggest API and return a validated subject id, or None."""
        try:
            resp = await self._client.get(
                "https://movie.douban.com/j/subject_suggest",
                params={"q": title},
            )
            resp.raise_for_status()
            candidates: list[dict[str, object]] = resp.json()
        except Exception as exc:  # noqa: BLE001 (intentional: never propagate)
            logger.warning("豆瓣建议接口失败(%s): %s", title, exc)
            return None

        if not isinstance(candidates, list) or not candidates:
            return None

        # Check up to first 3 candidates
        for i, candidate in enumerate(candidates[:3]):
            if not isinstance(candidate, dict):
                continue
            cand_year_str = str(candidate.get("year", ""))
            cand_year = _safe_int(cand_year_str)

            # Year validation: mismatch → skip; either side None → accept
            if expected_year is not None and cand_year is not None and cand_year != expected_year:
                    continue

            subject_id = candidate.get("id")
            if subject_id:
                return str(subject_id)

        return None

    async def _parse_subject(self, subject_id: str) -> DoubanSupplement | None:
        """Fetch and parse the subject detail page."""
        try:
            resp = await self._client.get(
                f"https://movie.douban.com/subject/{subject_id}/",
            )
            resp.raise_for_status()
            content = resp.content  # raw bytes — lxml handles encoding detection
        except Exception as exc:  # noqa: BLE001 (intentional: never propagate)
            logger.warning("豆瓣详情页请求失败(%s): %s", subject_id, exc)
            return None

        try:
            parser = etree.HTMLParser(encoding="utf-8")
            tree = etree.HTML(content, parser=parser)
        except Exception as exc:  # noqa: BLE001 (intentional: never propagate)
            logger.warning("豆瓣HTML解析失败(%s): %s", subject_id, exc)
            return None

        # Rating
        rating: float | None = None
        try:
            rating_elements = tree.xpath(_XPATH_RATING)
            if rating_elements:
                rating_text = (rating_elements[0].text or "").strip()
                if rating_text:
                    rating = float(rating_text)
        except (ValueError, TypeError) as exc:
            logger.debug("豆瓣评分解析失败(%s): %s", subject_id, exc)

        # Summary
        overview: str | None = None
        try:
            summary_elements = tree.xpath(_XPATH_SUMMARY)
            if summary_elements:
                text = (summary_elements[0].text or "").strip()
                if text:
                    # Collapse internal whitespace
                    overview = " ".join(text.split())
        except Exception as exc:  # noqa: BLE001 (intentional: never propagate)
            logger.debug("豆瓣简介解析失败(%s): %s", subject_id, exc)

        if overview is None and rating is None:
            return None

        return DoubanSupplement(overview=overview, rating=rating)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _safe_int(s: str) -> int | None:
    """Convert *s* to int, or None on failure."""
    try:
        return int(s)
    except (ValueError, TypeError):
        return None
