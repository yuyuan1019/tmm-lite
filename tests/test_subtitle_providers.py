"""Protocol-level tests for OpenSubtitles and SubDL clients."""

from __future__ import annotations

import json

import pytest
import respx

from app.scrapers.opensubtitles import BASE_URL as OPENSUBTITLES_BASE_URL
from app.scrapers.opensubtitles import (
    DEFAULT_USER_AGENT,
    OpenSubtitlesError,
    OpenSubtitlesScraper,
    SubtitleResult,
)
from app.scrapers.subdl import SubDLError, SubDLScraper


@pytest.mark.asyncio
async def test_opensubtitles_search_uses_imdb_id_and_browser_user_agent() -> None:
    scraper = OpenSubtitlesScraper("os-key")
    scraper._min_interval = 0.0
    payload = {
        "data": [
            {
                "attributes": {
                    "language": "zh-cn",
                    "ratings": 8.5,
                    "hearing_impaired": False,
                    "files": [{"file_id": 123, "file_name": "release.ass"}],
                }
            }
        ]
    }
    try:
        with respx.mock:
            route = respx.get(f"{OPENSUBTITLES_BASE_URL}/subtitles").respond(
                200, json=payload
            )
            results = await scraper.search(
                "Film", 2020, "zh-cn", imdb_id="tt1234567"
            )

        request = route.calls[0].request
        assert request.url.params["imdb_id"] == "1234567"
        assert request.url.params["languages"] == "zh-cn"
        assert "query" not in request.url.params
        assert "year" not in request.url.params
        assert request.headers["user-agent"] == DEFAULT_USER_AGENT
        assert results[0].download_url == "123"
        assert results[0].filename == "release.ass"
    finally:
        await scraper.aclose()


@pytest.mark.asyncio
async def test_opensubtitles_download_posts_file_id_to_download_endpoint() -> None:
    scraper = OpenSubtitlesScraper("os-key")
    scraper._min_interval = 0.0
    result = SubtitleResult(
        "opensubtitles", "zh-cn", "release.srt", "123"
    )
    download_link = "https://dl.opensubtitles.com/subtitles/release.srt"
    try:
        with respx.mock:
            route = respx.post(f"{OPENSUBTITLES_BASE_URL}/download").respond(
                200, json={"link": download_link}
            )
            file_route = respx.get(download_link).respond(200, content=b"subtitle")
            data = await scraper.download(result)

        assert json.loads(route.calls[0].request.content) == {"file_id": 123}
        assert "api-key" not in file_route.calls[0].request.headers
        assert data == b"subtitle"
    finally:
        await scraper.aclose()


@pytest.mark.asyncio
async def test_opensubtitles_http_failure_is_not_reported_as_empty() -> None:
    scraper = OpenSubtitlesScraper("os-key")
    scraper._min_interval = 0.0
    try:
        with respx.mock:
            respx.get(f"{OPENSUBTITLES_BASE_URL}/subtitles").respond(401)
            with pytest.raises(OpenSubtitlesError, match="HTTP 401"):
                await scraper.search("Film", 2020, "zh-cn")
    finally:
        await scraper.aclose()


@pytest.mark.asyncio
async def test_subdl_search_uses_official_endpoint_key_and_subtitles_array() -> None:
    scraper = SubDLScraper("subdl-key")
    scraper._min_interval = 0.0
    payload = {
        "status": True,
        "results": [{"name": "Film", "imdb_id": "tt1234567"}],
        "subtitles": [
            {
                "name": "archive.zip",
                "url": "/subtitle/archive.zip",
                "unpack_files": [
                    {
                        "name": "release.ass",
                        "url": "/subtitle/archive/file-1",
                        "language": "ZH",
                        "hi": False,
                    }
                ],
            }
        ],
    }
    try:
        with respx.mock:
            route = respx.get("https://api.subdl.com/api/v1/subtitles").respond(
                200, json=payload
            )
            results = await scraper.search(
                "Film", 2020, "ZH", imdb_id="tt1234567"
            )

        params = route.calls[0].request.url.params
        assert params["api_key"] == "subdl-key"
        assert params["imdb_id"] == "tt1234567"
        assert params["languages"] == "ZH"
        assert params["unpack"] == "1"
        assert "film_name" not in params
        assert len(results) == 1
        assert results[0].filename == "release.ass"
        assert results[0].download_url == "https://dl.subdl.com/subtitle/archive/file-1"
    finally:
        await scraper.aclose()


@pytest.mark.asyncio
async def test_subdl_api_error_is_not_reported_as_empty() -> None:
    scraper = SubDLScraper("subdl-key")
    scraper._min_interval = 0.0
    try:
        with respx.mock:
            respx.get("https://api.subdl.com/api/v1/subtitles").respond(
                200, json={"status": False, "error": "invalid api key"}
            )
            with pytest.raises(SubDLError, match="invalid api key"):
                await scraper.search("Film", 2020, "ZH")
    finally:
        await scraper.aclose()


@pytest.mark.asyncio
async def test_subtitle_clients_retain_configured_proxy() -> None:
    opensubtitles = OpenSubtitlesScraper("os-key", proxy="http://127.0.0.1:7890")
    subdl = SubDLScraper("subdl-key", proxy="http://127.0.0.1:7890")

    try:
        assert opensubtitles._proxy == "http://127.0.0.1:7890"
        assert subdl._proxy == "http://127.0.0.1:7890"
    finally:
        await opensubtitles.aclose()
        await subdl.aclose()
