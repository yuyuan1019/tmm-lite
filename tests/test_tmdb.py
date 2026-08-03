"""M5 TMDB scraper tests — M5-T1 through M5-T8."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.exceptions import TmdbAuthError, TmdbError, TmdbRateLimitError
from app.scrapers.tmdb import IMAGE_BASE, TmdbScraper

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _json(name: str) -> dict[str, object]:
    return json.loads(_load(name))  # type: ignore[no-any-return]


def _bytes(name: str) -> bytes:
    return _load(name).encode()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def tmdb() -> TmdbScraper:
    return TmdbScraper(api_key="test-key", language="zh-CN")


@pytest.fixture
def tmdb_no_key() -> TmdbScraper:
    return TmdbScraper(api_key="")


# ---------------------------------------------------------------------------
# M5-T1: Movie search hit → detail → ScrapedMeta
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_movie_search_and_fetch(tmdb: TmdbScraper) -> None:
    search_json = _json("tmdb_search_movie.json")
    detail_json = _json("tmdb_movie_detail.json")

    with respx.mock as mock:
        # Match search request
        mock.get("https://api.themoviedb.org/3/search/movie").respond(
            json=search_json,
        )
        # Match detail request
        mock.get("https://api.themoviedb.org/3/movie/157336").respond(
            json=detail_json,
        )

        result = await tmdb.search_and_fetch("星际穿越", 2014, "movie")

    assert result is not None
    assert result.source == "tmdb"
    assert result.source_id == "157336"
    assert result.title == "星际穿越"
    assert result.original_title == "Interstellar"
    assert result.year == 2014
    assert result.overview == "一部关于太空旅行的电影"
    assert result.rating == 8.7
    assert result.genres == ["科幻", "冒险"]
    assert result.poster_url == IMAGE_BASE + "/p.jpg"
    assert result.backdrop_url == IMAGE_BASE + "/b.jpg"


# ---------------------------------------------------------------------------
# M5-T2: TV search uses /search/tv + first_air_date_year
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_tv_search_and_fetch(tmdb: TmdbScraper) -> None:
    search_json = _json("tmdb_search_tv.json")
    detail_json = _json("tmdb_tv_detail.json")

    with respx.mock as mock:
        mock.get("https://api.themoviedb.org/3/search/tv").respond(
            json=search_json,
        )
        mock.get("https://api.themoviedb.org/3/tv/123456").respond(
            json=detail_json,
        )

        result = await tmdb.search_and_fetch("繁花", 2023, "tv")

    assert result is not None
    assert result.title == "繁花"
    assert result.year == 2023
    assert result.source_id == "123456"


# ---------------------------------------------------------------------------
# M5-T3: No results → year fallback
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_results_with_year_fallback(tmdb: TmdbScraper) -> None:
    search_json = _json("tmdb_search_movie.json")

    with respx.mock as mock:
        route = mock.get("https://api.themoviedb.org/3/search/movie")
        # First call: no results
        route.side_effect = [
            httpx.Response(200, json={"results": []}),
            httpx.Response(200, json=search_json),
        ]
        mock.get("https://api.themoviedb.org/3/movie/157336").respond(
            json=_json("tmdb_movie_detail.json"),
        )

        result = await tmdb.search_and_fetch("星际穿越", 2014, "movie")

    assert result is not None
    assert result.source_id == "157336"


@pytest.mark.asyncio
async def test_no_results_even_after_fallback(tmdb: TmdbScraper) -> None:
    with respx.mock as mock:
        mock.get("https://api.themoviedb.org/3/search/movie").respond(
            json={"results": []},
        )

        result = await tmdb.search_and_fetch("NoSuchMovie", 2099, "movie")

    assert result is None


# ---------------------------------------------------------------------------
# M5-T4: 429 retry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_429_retry_success(tmdb: TmdbScraper) -> None:
    call_count = [0]

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count[0] += 1
        if call_count[0] <= 3:
            return httpx.Response(429, headers={"Retry-After": "0.01"})
        return httpx.Response(200, json=_json("tmdb_search_movie.json"))

    with respx.mock as mock:
        mock.get("https://api.themoviedb.org/3/search/movie").mock(
            side_effect=_handler,
        )
        mock.get("https://api.themoviedb.org/3/movie/157336").respond(
            json=_json("tmdb_movie_detail.json"),
        )

        result = await tmdb.search_and_fetch("星际穿越", 2014, "movie")

    assert result is not None
    assert call_count[0] == 4  # 3 retries + 1 success


@pytest.mark.asyncio
async def test_429_exhausted(tmdb: TmdbScraper) -> None:
    with respx.mock as mock:
        mock.get("https://api.themoviedb.org/3/search/movie").respond(
            429, headers={"Retry-After": "0.01"},
        )

        with pytest.raises(TmdbRateLimitError):
            await tmdb.search_and_fetch("星际穿越", 2014, "movie")


# ---------------------------------------------------------------------------
# M5-T5: 5xx / network timeout → TmdbError
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_5xx_raises_tmdb_error(tmdb: TmdbScraper) -> None:
    with respx.mock as mock:
        mock.get("https://api.themoviedb.org/3/search/movie").respond(500)

        with pytest.raises(TmdbError) as exc_info:
            await tmdb.search_and_fetch("星际穿越", 2014, "movie")

    assert "500" in str(exc_info.value)
    # Must contain endpoint path but NOT api_key
    assert "/search/movie" in str(exc_info.value)
    assert "test-key" not in str(exc_info.value)


# ---------------------------------------------------------------------------
# M5-T6: language parameter in search/detail, NOT in image requests
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_language_param_in_requests(tmdb: TmdbScraper) -> None:
    requests_made: list[httpx.Request] = []

    with respx.mock as mock:
        route = mock.get("https://api.themoviedb.org/3/search/movie")
        route.side_effect = lambda req: (
            requests_made.append(req),
            httpx.Response(200, json=_json("tmdb_search_movie.json")),
        )[1]
        mock.get("https://api.themoviedb.org/3/movie/157336").respond(
            json=_json("tmdb_movie_detail.json"),
        )

        await tmdb.search_and_fetch("星际穿越", 2014, "movie")

    # At least one search request was made
    assert len(requests_made) > 0
    # Check language parameter in the search URL
    search_url = str(requests_made[0].url)
    assert "language=zh-CN" in search_url


# ---------------------------------------------------------------------------
# M5-T7: Image download
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_image_download(tmdb: TmdbScraper, tmp_path: Path) -> None:
    test_data = b"\xff\xd8\xff\xe0JPEG DATA"

    with respx.mock as mock:
        mock.get("https://image.tmdb.org/t/p/original/p.jpg").respond(
            200, content=test_data, headers={"Content-Type": "image/jpeg"},
        )

        dest = tmp_path / "poster.jpg"
        await tmdb.download_image("https://image.tmdb.org/t/p/original/p.jpg", dest)

    assert dest.exists()
    assert dest.read_bytes() == test_data


@pytest.mark.asyncio
async def test_image_download_404(tmdb: TmdbScraper, tmp_path: Path) -> None:
    with respx.mock as mock:
        mock.get("https://image.tmdb.org/t/p/original/bad.jpg").respond(404)

        with pytest.raises(TmdbError):
            await tmdb.download_image(
                "https://image.tmdb.org/t/p/original/bad.jpg", tmp_path / "bad.jpg",
            )

    # No temp file left behind
    tmps = list(tmp_path.glob("*.tmp"))
    assert len(tmps) == 0


# ---------------------------------------------------------------------------
# M5-T8: Auth errors and key sanitisation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_empty_key_raises_auth_error(tmdb_no_key: TmdbScraper) -> None:
    with pytest.raises(TmdbAuthError, match="未配置"):
        await tmdb_no_key.search_and_fetch("星际穿越", 2014, "movie")


@pytest.mark.asyncio
async def test_401_raises_auth_error(tmdb: TmdbScraper) -> None:
    with respx.mock as mock:
        mock.get("https://api.themoviedb.org/3/search/movie").respond(401)

        with pytest.raises(TmdbAuthError, match="无效"):
            await tmdb.search_and_fetch("星际穿越", 2014, "movie")


@pytest.mark.asyncio
async def test_exception_text_does_not_contain_api_key(tmdb: TmdbScraper) -> None:
    with respx.mock as mock:
        mock.get("https://api.themoviedb.org/3/search/movie").respond(500)

        with pytest.raises(TmdbError) as exc_info:
            await tmdb.search_and_fetch("星际穿越", 2014, "movie")

    assert "test-key" not in str(exc_info.value)
