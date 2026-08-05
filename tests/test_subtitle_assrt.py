"""ASSRT (assrt.net) subtitle scraper tests."""

from __future__ import annotations

import pytest
import respx

from app.scrapers.assrt import AssrtScraper
from app.scrapers.opensubtitles import SubtitleResult

_SEARCH_URL = "https://api.assrt.net/v1/sub/search"
_DETAIL_URL = "https://api.assrt.net/v1/sub/detail"
_FILE_URL = "http://file0.assrt.net/test/movie.srt"


@pytest.fixture
def scraper() -> AssrtScraper:
    s = AssrtScraper(token="test-token")
    s._min_interval = 0.0  # no rate-limit waiting in tests
    return s


@pytest.mark.asyncio
async def test_search_parses_results_sorted_by_score(scraper: AssrtScraper) -> None:
    payload = {
        "status": 0,
        "sub": {
            "subs": [
                {
                    "id": 602333,
                    "native_name": "洛东江大决战",
                    "videoname": "movie.1976.dvd",
                    "vote_score": 5,
                    "lang": {"desc": "中英双语", "langlist": {"langdou": True}},
                },
                {
                    "id": 602334,
                    "native_name": "另一个版本",
                    "vote_score": 1,
                    "lang": {"desc": "简体中文"},
                },
            ],
            "result": "succeed",
        },
    }
    with respx.mock:
        respx.get(_SEARCH_URL).respond(200, json=payload)
        results = await scraper.search("movie", 1976)

    assert [r.download_url for r in results] == ["602333", "602334"]  # score desc
    assert results[0].provider == "assrt"
    assert results[0].filename == "洛东江大决战"
    assert results[0].language == "中英双语"


@pytest.mark.asyncio
async def test_search_empty_results(scraper: AssrtScraper) -> None:
    with respx.mock:
        respx.get(_SEARCH_URL).respond(200, json={"status": 0, "sub": {"subs": []}})
        assert await scraper.search("nothing") == []


@pytest.mark.asyncio
async def test_search_nonzero_status_returns_empty(scraper: AssrtScraper) -> None:
    with respx.mock:
        respx.get(_SEARCH_URL).respond(200, json={"status": 30900})  # rate-limited
        assert await scraper.search("x") == []


@pytest.mark.asyncio
async def test_search_http_error_returns_empty(scraper: AssrtScraper) -> None:
    with respx.mock:
        respx.get(_SEARCH_URL).respond(500)
        assert await scraper.search("x") == []


@pytest.mark.asyncio
async def test_download_fetches_first_filelist_file(scraper: AssrtScraper) -> None:
    detail = {
        "status": 0,
        "sub": {
            "subs": [
                {
                    "id": 602333,
                    "filelist": [
                        {"f": "movie.srt", "s": "52KB", "url": _FILE_URL},
                        {"f": "movie.ass", "s": "10KB", "url": "http://file0.assrt.net/x.ass"},
                    ],
                }
            ]
        },
    }
    srt_bytes = b"1\n00:00:01,000 --> 00:00:02,000\nhello\n"
    with respx.mock:
        respx.get(_DETAIL_URL).respond(200, json=detail)
        respx.get(_FILE_URL).respond(200, content=srt_bytes)
        result = SubtitleResult(
            provider="assrt", language="chi", filename="movie", download_url="602333",
        )
        data = await scraper.download(result)

    assert data == srt_bytes


@pytest.mark.asyncio
async def test_download_raises_when_no_filelist(scraper: AssrtScraper) -> None:
    from app.exceptions import TmmError

    detail = {"status": 0, "sub": {"subs": [{"id": 602333, "filelist": []}]}}
    with respx.mock:
        respx.get(_DETAIL_URL).respond(200, json=detail)
        result = SubtitleResult(
            provider="assrt", language="chi", filename="movie", download_url="602333",
        )
        with pytest.raises(TmmError):
            await scraper.download(result)
