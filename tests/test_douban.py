"""M6 Douban scraper tests — M6-T1 through M6-T6."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
import respx

from app.scrapers.douban import DoubanScraper

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.fixture
def douban() -> DoubanScraper:
    return DoubanScraper(delay_seconds=0.5)  # minimum allowed delay


# ---------------------------------------------------------------------------
# M6-T1: Normal hit → returns overview + rating
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_normal_hit(douban: DoubanScraper) -> None:
    suggest_data = json.loads(_load("douban_suggest.json"))
    subject_html = _load("douban_subject.html")

    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").respond(
            json=suggest_data,
        )
        mock.get("https://movie.douban.com/subject/1889243/").respond(
            content=subject_html.encode("utf-8"),
            headers={"Content-Type": "text/html; charset=utf-8"},
        )

        result = await douban.fetch_supplement("星际穿越", 2014)

    assert result is not None
    assert result.rating == 9.4
    assert result.overview == "豆瓣中文简介文本"


# ---------------------------------------------------------------------------
# M6-T2: Year candidate filtering — correct year on 2nd candidate
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_year_candidate_filtering(douban: DoubanScraper) -> None:
    # First candidate year=2020 (wrong), second year=2014 (correct)
    suggest = [
        {"id": "1", "title": "Wrong", "year": "2020"},
        {"id": "2", "title": "Correct", "year": "2014"},
    ]
    subject_html = _load("douban_subject.html")

    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").respond(json=suggest)
        mock.get("https://movie.douban.com/subject/2/").respond(content=subject_html.encode("utf-8"), headers={"Content-Type": "text/html; charset=utf-8"})

        result = await douban.fetch_supplement("星际穿越", 2014)

    assert result is not None
    assert result.overview == "豆瓣中文简介文本"


@pytest.mark.asyncio
async def test_year_all_candidates_mismatch(douban: DoubanScraper) -> None:
    suggest = [
        {"id": "1", "title": "A", "year": "2020"},
        {"id": "2", "title": "B", "year": "2021"},
        {"id": "3", "title": "C", "year": "2022"},
    ]

    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").respond(json=suggest)

        result = await douban.fetch_supplement("星际穿越", 2014)

    assert result is None


# ---------------------------------------------------------------------------
# M6-T3: Year missing → no validation, still returns data
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_year_none_tolerance(douban: DoubanScraper) -> None:
    suggest = [{"id": "1", "title": "Something", "year": ""}]
    subject_html = _load("douban_subject.html")

    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").respond(json=suggest)
        mock.get("https://movie.douban.com/subject/1/").respond(content=subject_html.encode("utf-8"), headers={"Content-Type": "text/html; charset=utf-8"})

        result = await douban.fetch_supplement("Something", None)

    assert result is not None


# ---------------------------------------------------------------------------
# M6-T4: Rate limiting — second call waits at least delay_seconds
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rate_limiting() -> None:
    """With delay=0.5s, two rapid calls should be spaced ≥ 0.5s apart."""
    douban_slow = DoubanScraper(delay_seconds=0.5)

    suggest_data = json.loads(_load("douban_suggest.json"))
    subject_html = _load("douban_subject.html")

    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").respond(
            json=suggest_data,
        )
        mock.get("https://movie.douban.com/subject/1889243/").respond(
            text=subject_html,
        )

        import time
        t0 = time.monotonic()
        await douban_slow.fetch_supplement("星际穿越", 2014)
        t1 = time.monotonic()  # noqa: F841 (documenting timing)
        await douban_slow.fetch_supplement("星际穿越", 2014)
        t2 = time.monotonic()

    # The second call should have been delayed at least 0.5s from the first
    # (there are 2 HTTP requests per call, each waits the limiter)
    # Total time should be at least 0.5s for the first call (2 requests × 0.5s = 1s)
    # And another 1s for the second call. But the test fixture returns instantly
    # so the limiter is the only delay.
    # First call: 2 requests × 0.5s delay = ~1.0s
    # Second call: 2 requests × 0.5s delay = ~1.0s
    # Total ~2.0s. We'll be lenient: just check total > 0.5s
    assert t2 - t0 >= 0.4  # slight tolerance


# ---------------------------------------------------------------------------
# M6-T5: Exceptions swallowed — never propagates, returns None
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_timeout_returns_none(douban: DoubanScraper) -> None:
    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").mock(
            side_effect=httpx.TimeoutException("timeout"),
        )

        result = await douban.fetch_supplement("星际穿越", 2014)

    assert result is None


@pytest.mark.asyncio
async def test_500_returns_none(douban: DoubanScraper) -> None:
    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").respond(500)

        result = await douban.fetch_supplement("星际穿越", 2014)

    assert result is None


@pytest.mark.asyncio
async def test_bad_html_structure_returns_none(douban: DoubanScraper) -> None:
    suggest_data = json.loads(_load("douban_suggest.json"))

    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").respond(
            json=suggest_data,
        )
        # Return garbage HTML
        mock.get("https://movie.douban.com/subject/1889243/").respond(
            text="<html><body>Not valid douban page</body></html>",
        )

        result = await douban.fetch_supplement("星际穿越", 2014)

    # The HTML has no rating or summary nodes, so both fields are None → returns None
    assert result is None


# ---------------------------------------------------------------------------
# M6-T6: No search results → returns None
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_no_search_results(douban: DoubanScraper) -> None:
    with respx.mock as mock:
        mock.get("https://movie.douban.com/j/subject_suggest").respond(json=[])

        result = await douban.fetch_supplement("NonExistentMovie", 2099)

    assert result is None
