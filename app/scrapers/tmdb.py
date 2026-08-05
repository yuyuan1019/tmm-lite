"""TMDB scraper (M5).

Wraps the TMDB v3 REST API: search → detail → :class:`ScrapedMeta`.
Handles 429 rate-limiting with ``Retry-After`` backoff, authentication errors,
and image downloads.

See implementation spec §7.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

import httpx

from app.exceptions import TmdbAuthError, TmdbError, TmdbRateLimitError
from app.scrapers.base import ScrapedMeta

logger = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/original"


class _RateLimiter:
    """Async rate limiter enforcing a minimum interval between API calls."""

    def __init__(self, min_interval: float) -> None:
        self._min = max(0.0, min_interval)
        self._last: float = 0.0  # monotonic
        self._lock = asyncio.Lock()

    async def wait(self) -> None:
        """Block until the minimum interval since the last call has elapsed."""
        if self._min <= 0:
            return
        async with self._lock:
            now = time.monotonic()
            delta = self._last + self._min - now
            if delta > 0:
                await asyncio.sleep(delta)
            self._last = time.monotonic()


class TmdbScraper:
    """Async TMDB API client.

    Args:
        api_key: TMDB v3 API key (may be empty — auth-protected calls will fail).
        language: ISO 639-1 language code sent with each search/detail request.
        proxy: Optional ``http(s)``/``socks5`` proxy URL applied to both the
            API and image clients.  Empty string / None means no proxy.
    """

    def __init__(
        self,
        api_key: str,
        language: str = "zh-CN",
        proxy: str | None = None,
        min_interval: float = 0.0,
    ) -> None:
        self._api_key = api_key
        self._language = language
        proxy = proxy or None
        self._proxy = proxy
        self._limiter = _RateLimiter(min_interval)
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0), proxy=proxy,
        )
        self._image_client = httpx.AsyncClient(
            timeout=httpx.Timeout(30.0), proxy=proxy,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search_and_fetch(
        self, title: str, year: int | None, media_type: str,
    ) -> ScrapedMeta | None:
        """Search TMDB for *title* and fetch full details.

        Returns ``None`` when no results are found (the caller should mark the
        item as ``failed`` with an appropriate message).
        """
        self._check_key()

        search_id = await self._search(title, year, media_type)
        if search_id is None:
            return None

        detail = await self._detail(search_id, media_type)
        meta = self._map_to_meta(media_type, detail)
        if meta.imdb_id is None:
            # TMDB TV details often omit imdb_id; the external_ids endpoint has it.
            ext = await self._external_ids(search_id, media_type)
            meta.imdb_id = _str_or_none(ext.get("imdb_id"))
        return meta

    async def fetch_by_id(self, tmdb_id: int, media_type: str) -> ScrapedMeta:
        """Fetch and map metadata for a specific TMDB id (manual match)."""
        self._check_key()
        detail = await self._detail(tmdb_id, media_type)
        meta = self._map_to_meta(media_type, detail)
        if meta.imdb_id is None:
            ext = await self._external_ids(tmdb_id, media_type)
            meta.imdb_id = _str_or_none(ext.get("imdb_id"))
        return meta

    async def search_candidates(
        self, title: str, media_type: str, limit: int = 8,
    ) -> list[dict[str, object]]:
        """Return up to *limit* TMDB search candidates for the manual-match dialog."""
        self._check_key()
        endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
        data = await self._get_json(endpoint, self._params(query=title))
        raw_results = data.get("results", [])
        results = raw_results if isinstance(raw_results, list) else []
        date_field = "release_date" if media_type == "movie" else "first_air_date"
        candidates: list[dict[str, object]] = []
        for r in results[:limit]:
            if not isinstance(r, dict):
                continue
            date_str = _str_or_none(r.get(date_field))
            year = date_str[:4] if date_str and len(date_str) >= 4 else ""
            poster = r.get("poster_path")
            candidates.append({
                "id": r.get("id"),
                "title": r.get("title") or r.get("name") or "",
                "original_title": r.get("original_title") or r.get("original_name"),
                "year": year,
                "poster": (IMAGE_BASE + poster) if isinstance(poster, str) else None,
            })
        return candidates

    async def fetch_image(self, url: str) -> bytes:
        """Download an image from *url* and return its raw bytes.

        Shares the same rate limiter as the API calls, so image (CDN)
        downloads are paced by ``tmdb_delay_seconds`` too.
        """
        await self._limiter.wait()
        clean_url = _sanitise_url(url)
        try:
            resp = await self._image_client.get(clean_url)
            if resp.status_code != 200:
                raise TmdbError(f"TMDB 图片下载失败 HTTP {resp.status_code}: {clean_url}")
            return resp.content
        except TmdbError:
            raise
        except Exception as exc:
            raise TmdbError(f"TMDB 图片下载失败: {type(exc).__name__}: {clean_url}") from exc

    async def download_image(self, url: str, dest: Path) -> None:
        """Download an image from *url* and atomically write it to *dest*."""
        data = await self.fetch_image(url)
        tmp = dest.with_name(dest.name + ".tmp")
        try:
            with open(tmp, "wb") as f:  # noqa: ASYNC230
                f.write(data)
            tmp.replace(dest)
        except Exception:
            if tmp.exists():
                tmp.unlink()
            raise

    async def aclose(self) -> None:
        """Close the underlying HTTP clients."""
        await self._client.aclose()
        await self._image_client.aclose()

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _check_key(self) -> None:
        if not self._api_key:
            raise TmdbAuthError("TMDB API Key 未配置")

    def _params(self, **extra: object) -> dict[str, str | int | float]:
        base: dict[str, str | int | float] = {
            "api_key": self._api_key,
            "language": self._language,
        }
        for k, v in extra.items():
            if isinstance(v, (str, int, float)):
                base[k] = v
            elif v is not None:
                base[k] = str(v)
        return base

    async def _search(
        self, title: str, year: int | None, media_type: str,
    ) -> int | None:
        """Search TMDB; return the first result's id, or None.

        Multiple candidate queries are tried from most to least specific: the
        exact title, then the title with a trailing Chinese sequence number
        stripped ("奇异博士1" → "奇异博士", since TMDB titles omit the "1").
        Each query is tried with the year filter first, then without it.
        """
        endpoint = "/search/movie" if media_type == "movie" else "/search/tv"
        year_param_name = "year" if media_type == "movie" else "first_air_date_year"

        candidates = [title]
        stripped = _strip_cjk_sequence_number(title)
        if stripped and stripped != title:
            candidates.append(stripped)

        years = [year, None] if year is not None else [None]

        for query in candidates:
            for y in years:
                params = self._params(query=query)
                if y is not None:
                    params[year_param_name] = y
                data = await self._get_json(endpoint, params)
                results = data.get("results", [])
                if isinstance(results, list) and results:
                    first = results[0]
                    if isinstance(first, dict):
                        return int(first["id"])  # type: ignore[arg-type]
        return None

    async def _detail(self, item_id: int, media_type: str) -> dict[str, object]:
        """Fetch full details for an item."""
        if media_type == "movie":
            endpoint = f"/movie/{item_id}"
        else:
            endpoint = f"/tv/{item_id}"
        return await self._get_json(endpoint, self._params())

    async def _external_ids(self, item_id: int, media_type: str) -> dict[str, object]:
        """Fetch the item's external IDs (some titles carry imdb_id here only)."""
        if media_type == "movie":
            endpoint = f"/movie/{item_id}/external_ids"
        else:
            endpoint = f"/tv/{item_id}/external_ids"
        return await self._get_json(endpoint, self._params())

    async def _get_json(
        self, endpoint: str, params: dict[str, str | int | float],
    ) -> dict[str, object]:
        """GET *endpoint* with automatic 429 retry (up to 4 total attempts)."""
        max_attempts = 4
        last_status: int | None = None

        for attempt in range(max_attempts):
            await self._limiter.wait()
            try:
                resp = await self._client.get(
                    f"{BASE_URL}{endpoint}", params=params,
                )
            except Exception as exc:
                raise TmdbError(
                    f"TMDB 网络错误: {type(exc).__name__}: {endpoint}"
                ) from exc

            if resp.status_code == 200:
                return resp.json()  # type: ignore[no-any-return]

            if resp.status_code == 401:
                raise TmdbAuthError("TMDB API Key 无效")

            if resp.status_code == 429:
                last_status = 429
                if attempt == max_attempts - 1:
                    raise TmdbRateLimitError(
                        f"TMDB 限流: {endpoint} ({max_attempts} 次尝试后仍被限流)"
                    )
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                logger.warning(
                    "TMDB 429 on %s, sleeping %.1fs (attempt %d/%d)",
                    endpoint, retry_after, attempt + 1, max_attempts,
                )
                await asyncio.sleep(retry_after)
                continue

            # Other 4xx / 5xx
            raise TmdbError(f"TMDB HTTP {resp.status_code}: {endpoint}")

        # Should not reach here
        raise TmdbError(f"TMDB HTTP {last_status}: {endpoint}")

    def _map_to_meta(
        self, media_type: str, detail: dict[str, object],
    ) -> ScrapedMeta:
        """Map a TMDB detail response to :class:`ScrapedMeta`."""
        if media_type == "movie":
            title = str(detail.get("title") or detail.get("original_title", ""))
            original_title = _str_or_none(detail.get("original_title"))
            date_field = "release_date"
        else:
            title = str(detail.get("name") or detail.get("original_name", ""))
            original_title = _str_or_none(detail.get("original_name"))
            date_field = "first_air_date"

        # Year
        date_str = _str_or_none(detail.get(date_field))
        year: int | None = None
        if date_str and len(date_str) >= 4:
            try:
                year = int(date_str[:4])
            except (ValueError, TypeError):
                pass

        # Rating
        raw_rating = detail.get("vote_average")
        rating: float | None = None
        if isinstance(raw_rating, (int, float)) and raw_rating != 0:
            rating = round(float(raw_rating), 1)

        # Overview
        overview = _str_or_none(detail.get("overview"))

        # Genres
        genres_raw = detail.get("genres", [])
        genres: list[str] = []
        if isinstance(genres_raw, list):
            for g in genres_raw:
                if isinstance(g, dict):
                    name = g.get("name")
                    if isinstance(name, str):
                        genres.append(name)

        # Images
        poster_path = _str_or_none(detail.get("poster_path"))
        backdrop_path = _str_or_none(detail.get("backdrop_path"))

        return ScrapedMeta(
            source="tmdb",
            source_id=str(detail.get("id", "")),
            title=title,
            original_title=original_title,
            year=year,
            overview=overview,
            rating=rating,
            genres=genres,
            poster_url=(IMAGE_BASE + poster_path) if poster_path else None,
            backdrop_url=(IMAGE_BASE + backdrop_path) if backdrop_path else None,
            imdb_id=_str_or_none(detail.get("imdb_id")),
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_cjk_sequence_number(title: str) -> str:
    """Strip a trailing sequence number from a Chinese title.

    e.g. ``"奇异博士1"`` → ``"奇异博士"`` (TMDB titles omit the ``"1"``).
    Only matches a digit-run immediately after a CJK character, so English
    titles and titles like ``"猎杀T34"`` are left unchanged.

    Also handles digits after CJK mid-title:
    ``"雷神1 索尔"`` → ``"雷神 索尔"``
    """
    t = title.strip()
    # Pattern 1: trailing digits after CJK — "奇异博士1" → "奇异博士"
    m = re.match(r"^(.*[一-鿿])(\d+)$", t)
    if m:
        return m.group(1)
    # Pattern 2: CJK+digits mid-title before a space/separator
    # "雷神1 索尔" → "雷神 索尔", "雷神3：诸神黄昏" → "雷神 诸神黄昏"
    result = re.sub(r"(?<=[一-鿿])\d+(?=[\s.．、：:·\-—])", "", t)
    result = re.sub(r"\s+", " ", result).strip()
    if result and result != t:
        return result
    return title


def _parse_retry_after(header: str | None) -> float:
    """Parse ``Retry-After`` header; default to 2 seconds."""
    if header is None:
        return 2.0
    try:
        return float(header)
    except (ValueError, TypeError):
        return 2.0


def _str_or_none(value: object) -> str | None:
    """Return *value* as a non-empty string, or None."""
    if value is None:
        return None
    s = str(value).strip()
    return s if s else None


def _sanitise_url(url: str) -> str:
    """Remove ``api_key`` query parameter from *url*."""
    if "api_key=" not in url:
        return url
    base, _, qs = url.partition("?")
    parts = qs.split("&")
    clean_parts = [p for p in parts if not p.startswith("api_key=")]
    if clean_parts:
        return base + "?" + "&".join(clean_parts)
    return base
