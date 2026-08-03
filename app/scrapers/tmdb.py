"""TMDB scraper (M5).

Wraps the TMDB v3 REST API: search → detail → :class:`ScrapedMeta`.
Handles 429 rate-limiting with ``Retry-After`` backoff, authentication errors,
and image downloads.

See implementation spec §7.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx

from app.exceptions import TmdbAuthError, TmdbError, TmdbRateLimitError
from app.scrapers.base import ScrapedMeta

logger = logging.getLogger(__name__)

BASE_URL = "https://api.themoviedb.org/3"
IMAGE_BASE = "https://image.tmdb.org/t/p/original"


class TmdbScraper:
    """Async TMDB API client.

    Args:
        api_key: TMDB v3 API key (may be empty — auth-protected calls will fail).
        language: ISO 639-1 language code sent with each search/detail request.
    """

    def __init__(self, api_key: str, language: str = "zh-CN") -> None:
        self._api_key = api_key
        self._language = language
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(15.0))
        self._image_client = httpx.AsyncClient(timeout=httpx.Timeout(30.0))

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
        return self._map_to_meta(media_type, detail)

    async def fetch_image(self, url: str) -> bytes:
        """Download an image from *url* and return its raw bytes."""
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
        """Search TMDB; return the first result's id, or None."""
        if media_type == "movie":
            endpoint = "/search/movie"
            params = self._params(query=title)
            if year is not None:
                params["year"] = year
            year_param_name = "year"
        else:
            endpoint = "/search/tv"
            params = self._params(query=title)
            if year is not None:
                params["first_air_date_year"] = year
            year_param_name = "first_air_date_year"

        # First attempt (with year if provided)
        data = await self._get_json(endpoint, params)
        results = data.get("results", [])
        if isinstance(results, list) and results:
            first = results[0]
            if isinstance(first, dict):
                return int(first["id"])  # type: ignore[arg-type]

        # Fallback: retry without year (if year was provided)
        if year is not None:
            params.pop(year_param_name, None)
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

    async def _get_json(
        self, endpoint: str, params: dict[str, str | int | float],
    ) -> dict[str, object]:
        """GET *endpoint* with automatic 429 retry (up to 4 total attempts)."""
        max_attempts = 4
        last_status: int | None = None

        for attempt in range(max_attempts):
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
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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
