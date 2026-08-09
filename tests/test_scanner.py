"""M7 scanner tests — M7-T1 through M7-T21."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from app.config import AppConfig
from app.database import (
    AppMeta,
    Library,
    MediaItem,
    ScrapeLog,
    create_session_factory,
    init_db,
)
from app.exceptions import ScanBusyError, TmdbAuthError
from app.scanner import (
    ScanRunner,
    contains_video,
    normalize_path,
)
from app.scrapers.base import ScrapedMeta
from app.scrapers.douban import DoubanScraper
from app.scrapers.tmdb import TmdbScraper

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_config(**overrides: object) -> AppConfig:
    defaults: dict[str, object] = {
        "tmdb_api_key": "test-key",
        "use_douban": True,
        "douban_delay_seconds": 0.5,
        "overwrite_existing_nfo": False,
        "language": "zh-CN",
        "schedule_cron": "0 4 * * *",
    }
    defaults.update(overrides)
    return AppConfig(**defaults)  # type: ignore[arg-type]


def _add_library(sess: Session, name: str, path: str, media_type: str) -> Library:
    lib = Library(name=name, path=path, media_type=media_type)
    sess.add(lib)
    sess.commit()
    return lib


def _make_movie_tree(base: Path, *names: str) -> list[Path]:
    """Create movie folders with placeholder .mkv files."""
    paths: list[Path] = []
    for name in names:
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "placeholder.mkv").write_text("fake")
        paths.append(folder)
    return paths


def _make_tv_tree(base: Path, *names: str) -> list[Path]:
    """Create TV show folders."""
    paths: list[Path] = []
    for name in names:
        folder = base / name
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "placeholder.mkv").write_text("fake")
        paths.append(folder)
    return paths


def _mock_tmdb() -> MagicMock:
    tmdb = MagicMock(spec=TmdbScraper)
    tmdb.search_and_fetch = AsyncMock()
    tmdb.download_image = AsyncMock()
    tmdb.fetch_image = AsyncMock(return_value=b"\x89PNG\r\n")
    tmdb.aclose = AsyncMock()
    return tmdb


def _mock_douban() -> MagicMock:
    douban = MagicMock(spec=DoubanScraper)
    douban.fetch_supplement = AsyncMock(return_value=None)
    douban.aclose = AsyncMock()
    return douban


def _mock_meta(title: str = "Test Movie", **overrides: object) -> ScrapedMeta:
    defaults: dict[str, object] = {
        "source": "tmdb",
        "source_id": "999",
        "title": title,
        "original_title": None,
        "year": 2020,
        "overview": "A test movie.",
        "rating": 7.5,
        "genres": ["Action"],
        "poster_url": "https://example.com/p.jpg",
        "backdrop_url": None,
    }
    defaults.update(overrides)
    return ScrapedMeta(**defaults)  # type: ignore[arg-type]


class _RunnerHarness:
    """Holds all the pieces needed for scanner tests."""

    def __init__(self, tmp_path: Path, config: AppConfig | None = None):
        db_path = tmp_path / "tmm-lite.db"
        engine = init_db(db_path)
        self.factory = create_session_factory(engine)
        self.config = config or _make_config()
        self.tmdb = _mock_tmdb()
        self.douban = _mock_douban()
        self.runner = ScanRunner(self.factory, self.config, self.tmdb, self.douban)

    def session(self) -> Session:
        return self.factory()


def _setup(tmp_path: Path, config: AppConfig | None = None) -> _RunnerHarness:
    return _RunnerHarness(tmp_path, config)


# ---------------------------------------------------------------------------
# M7-T1: First full scan — all items matched
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_first_full_scan_all_matched(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Movie A (2020)", "Movie B (2021)", "Movie C (2022)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.side_effect = [
        _mock_meta(title="Movie A", year=2020, source_id="1"),
        _mock_meta(title="Movie B", year=2021, source_id="2"),
        _mock_meta(title="Movie C", year=2022, source_id="3"),
    ]

    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    log = await runner.run_full()

    assert log.total == 3
    assert log.matched == 3
    assert log.failed == 0

    # Verify items are matched
    with sess:
        items = sess.query(MediaItem).all()
        assert len(items) == 3
        for item in items:
            assert item.status == "matched"


# ---------------------------------------------------------------------------
# M7-T2: Idempotent re-scan
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_idempotent_rescan(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta(title="Film", year=2020)

    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # First scan
    await runner.run_full()
    tmdb.search_and_fetch.assert_called_once()

    # Second scan
    tmdb.search_and_fetch.reset_mock()
    log2 = await runner.run_full()

    # No API calls because matched items are skipped
    tmdb.search_and_fetch.assert_not_called()
    assert log2.total == 0
    assert log2.matched == 0

    # Still only 1 item
    with sess:
        count = sess.query(MediaItem).count()
        assert count == 1


# ---------------------------------------------------------------------------
# M7-T3: matched items skipped
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_matched_items_skipped(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    await runner.run_full()
    assert tmdb.search_and_fetch.call_count == 1

    # Re-scan with overwrite=false → no API calls
    tmdb.search_and_fetch.reset_mock()
    await runner.run_full()
    tmdb.search_and_fetch.assert_not_called()


# ---------------------------------------------------------------------------
# M7-T4: NFO status matrix
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_nfo_status_matrix_pending_with_nfo(tmp_path: Path) -> None:
    """pending item with existing NFO → skipped, becomes matched."""
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    folders = _make_movie_tree(movies_dir, "Film (2020)")
    # Pre-create an NFO
    (folders[0] / "movie.nfo").write_text("<movie/>")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    await runner.run_full()

    # TMDB should NOT have been called (NFO skip)
    tmdb.search_and_fetch.assert_not_called()

    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        assert item.status == "matched"


@pytest.mark.asyncio
async def test_nfo_status_matrix_failed_retry(tmp_path: Path) -> None:
    """failed item with existing NFO → still retried."""
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    folders = _make_movie_tree(movies_dir, "Film (2020)")
    # NO NFO initially — so TMDB is called and can fail

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    # First run: TMDB fails (no NFO → item enters API queue)
    tmdb.search_and_fetch.side_effect = Exception("fail")
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")
    await runner.run_full()

    # Now item is failed. Create an NFO — it should NOT prevent retry.
    (folders[0] / "movie.nfo").write_text("<movie/>")

    # Re-run: failed item should retry despite existing NFO
    tmdb.search_and_fetch.reset_mock()
    tmdb.search_and_fetch.side_effect = None
    tmdb.search_and_fetch.return_value = _mock_meta()
    await runner.run_full()

    # TMDB should be called again (failed status ignores NFO for retry)
    tmdb.search_and_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# M7-T5: Parse failure → manual_needed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parse_failure_manual_needed(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "1080p.x264-GROUP")  # pure noise → no title

    h = _setup(tmp_path)
    runner = h.runner; 

    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    log = await runner.run_full()

    # Should not enter queue, not count as failed
    assert log.total == 0

    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        assert item.status == "manual_needed"


# ---------------------------------------------------------------------------
# M7-T6: Disk deletion → missing
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_disk_deletion_missing(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    folders = _make_movie_tree(movies_dir, "Film A (2020)", "Film B (2021)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # First scan
    await runner.run_full()

    # Delete one folder
    import shutil
    shutil.rmtree(folders[0])

    # Re-scan
    await runner.run_full()

    with sess:
        items = sess.query(MediaItem).order_by(MediaItem.id).all()
        assert len(items) == 2
        statuses = {item.folder_path: item.status for item in items}
        missing_path = normalize_path(str(folders[0]))
        assert statuses.get(missing_path) == "missing" or any(
            s == "missing" for s in statuses.values()
        )


# ---------------------------------------------------------------------------
# M7-T7: missing item recovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_missing_recovery(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    folders = _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # First scan
    await runner.run_full()

    # Delete and re-scan → missing
    import shutil
    shutil.rmtree(folders[0])
    await runner.run_full()

    # Restore
    _make_movie_tree(movies_dir, "Film (2020)")
    # Also create NFO so it goes pending→matched
    (folders[0] / "movie.nfo").write_text("<movie/>")

    log = await runner.run_full()
    assert log.matched == 1

    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        assert item.status == "matched"


# ---------------------------------------------------------------------------
# M7-T8: Single item failure isolation
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_single_item_failure_isolation(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Good A (2020)", "Bad (2021)", "Good C (2022)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    call_count = [0]

    async def _side_effect(title: str, year: object, media_type: str) -> ScrapedMeta | None:
        call_count[0] += 1
        if "Bad" in title:
            raise Exception("Scrape failure")  # noqa: TRY002
        return _mock_meta(title=title)

    tmdb.search_and_fetch.side_effect = _side_effect
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    log = await runner.run_full()

    assert log.matched == 2
    assert log.failed == 1
    assert call_count[0] == 3  # All 3 attempted

    with sess:
        items = sess.query(MediaItem).all()
        statuses = {item.parsed_title: item.status for item in items}
        assert statuses.get("Good A") == "matched"
        assert statuses.get("Good C") == "matched"
        assert statuses.get("Bad") == "failed"


# ---------------------------------------------------------------------------
# M7-T9: Douban failure doesn't affect main flow
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_douban_failure_does_not_affect(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb; douban = h.douban

    tmdb.search_and_fetch.return_value = _mock_meta()
    douban.fetch_supplement.side_effect = Exception("Douban crash")
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.matched == 1  # Still succeeds despite douban failure


# ---------------------------------------------------------------------------
# M7-T10: Douban override
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_douban_override_applied(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb; douban = h.douban

    tmdb.search_and_fetch.return_value = _mock_meta(overview="TMDB overview", rating=7.0)

    from app.scrapers.douban import DoubanSupplement
    douban.fetch_supplement.return_value = DoubanSupplement(
        overview="Douban overview", rating=8.5,
    )
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    await runner.run_full()

    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        assert item.overview == "Douban overview"
        assert item.rating == 8.5


# ---------------------------------------------------------------------------
# M7-T11: Task mutex
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_task_mutex_rejects_concurrent(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    # Make TMDB slow
    event = asyncio.Event()
    async def _slow(title: str, year: object, media_type: str) -> ScrapedMeta:
        await event.wait()
        return _mock_meta()

    tmdb.search_and_fetch.side_effect = _slow
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # Start first scan in background
    task1 = runner.start_full_background()

    # Second call should be rejected
    with pytest.raises(ScanBusyError):
        await runner.run_full()

    # Clean up
    event.set()
    await task1


@pytest.mark.asyncio
async def test_background_rescrape_claims_before_task_is_scheduled(tmp_path: Path) -> None:
    """A background rescrape owns the runner before control returns to the loop."""
    h = _setup(tmp_path)
    release = asyncio.Event()

    async def blocked(*args: object, **kwargs: object) -> MediaItem:
        await release.wait()
        return MediaItem(
            id=1,
            library_id=1,
            media_type="movie",
            folder_path="/x",
            status="matched",
        )

    h.runner._rescrape_item_impl = blocked  # type: ignore[method-assign]

    first = h.runner.start_rescrape_item_background(1)
    assert h.runner.is_running is True
    with pytest.raises(ScanBusyError):
        h.runner.start_rescrape_item_background(2)
    assert h.runner._current_task is first

    release.set()
    await first
    assert h.runner.is_running is False
    assert h.runner._current_task is None


@pytest.mark.asyncio
async def test_rejected_background_start_does_not_create_coroutine(tmp_path: Path) -> None:
    """A busy rejection happens before invoking the coroutine factory."""
    h = _setup(tmp_path)
    release = asyncio.Event()
    factory_calls = 0

    async def blocked() -> ScrapeLog:
        await release.wait()
        return ScrapeLog(total=0, matched=0, failed=0)

    def rejected_factory() -> object:
        nonlocal factory_calls
        factory_calls += 1
        return blocked()

    first = h.runner._start_background(blocked)
    with pytest.raises(ScanBusyError):
        h.runner._start_background(rejected_factory)  # type: ignore[arg-type]

    assert factory_calls == 0
    release.set()
    await first


@pytest.mark.asyncio
async def test_stop_cancels_claimed_background_rescrape(tmp_path: Path) -> None:
    """stop() targets a rescrape task claimed synchronously by the shared starter."""
    h = _setup(tmp_path)
    entered = asyncio.Event()

    async def blocked(*args: object, **kwargs: object) -> MediaItem:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    h.runner._rescrape_item_impl = blocked  # type: ignore[method-assign]
    task = h.runner.start_rescrape_item_background(1)
    await entered.wait()

    assert h.runner.stop() is True
    with pytest.raises(asyncio.CancelledError):
        await task
    assert h.runner.is_running is False
    assert h.runner._current_task is None


# ---------------------------------------------------------------------------
# M7-T12: rescrape forces re-scrape
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rescrape_forces_rescrape(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # First scan → matched
    await runner.run_full()
    assert tmdb.search_and_fetch.call_count == 1

    # Create NFO so re-scan would skip it
    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        nfo_dir = Path(item.folder_path)
        (nfo_dir / "movie.nfo").write_text("<movie/>")

    # Rescrape single item (ignores NFO)
    tmdb.search_and_fetch.reset_mock()
    tmdb.search_and_fetch.return_value = _mock_meta()
    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        await runner.rescrape_item(item.id)

    tmdb.search_and_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# M7-T13: Image failure → item failed
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_image_failure_item_failed(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    folders = _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta(poster_url="https://x.com/p.jpg")
    tmdb.fetch_image.side_effect = Exception("Download failed")
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.failed == 1

    # No NFO should exist (NFO is written after images)
    assert not (folders[0] / "movie.nfo").exists()


# ---------------------------------------------------------------------------
# M7-T14: ScrapeLog statistics
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_scrapelog_statistics(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Good (2020)", "Bad (2021)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    async def _side(title: str, year: object, media_type: str) -> ScrapedMeta | None:
        if "Bad" in title:
            raise Exception("fail")  # noqa: TRY002
        return _mock_meta(title=title)

    tmdb.search_and_fetch.side_effect = _side
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.total == 2
    assert log.matched == 1
    assert log.failed == 1
    assert log.detail is not None
    assert "Bad" in log.detail


# ---------------------------------------------------------------------------
# M7-T15: Movie library: no video = no item
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_movie_library_no_video_no_item(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    # Create folder without video files
    empty = movies_dir / "Empty Folder (2020)"
    empty.mkdir()

    h = _setup(tmp_path)
    runner = h.runner; 

    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    await runner.run_full()

    with sess:
        count = sess.query(MediaItem).count()
        assert count == 0  # No video → no item


# ---------------------------------------------------------------------------
# M7-T16: Library inaccessible
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_library_inaccessible(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess,"Good Lib", str(movies_dir), "movie")
    with h.session() as sess:
        _add_library(sess,"Bad Lib", "/nonexistent/path", "movie")

    log = await runner.run_full()
    assert "库扫描失败" in (log.detail or "")

    # Good lib items should still be processed
    with sess:
        items = sess.query(MediaItem).filter(MediaItem.library_id == 1).all()
        assert len(items) == 1
        assert items[0].status == "matched"


# ---------------------------------------------------------------------------
# M7-T17: Overwrite matched items
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_overwrite_matched_items(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    config = _make_config(overwrite_existing_nfo=False)
    h = _setup(tmp_path, config=config)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # First scan with overwrite=false → matched
    await runner.run_full()
    assert tmdb.search_and_fetch.call_count == 1

    # Now enable overwrite
    tmdb.search_and_fetch.reset_mock()
    tmdb.search_and_fetch.return_value = _mock_meta(title="New Title")
    config2 = _make_config(overwrite_existing_nfo=True)
    new_tmdb = _mock_tmdb()
    new_tmdb.search_and_fetch.return_value = _mock_meta(title="New Title")
    new_tmdb.download_image = AsyncMock()
    new_tmdb.fetch_image = AsyncMock(return_value=b"\x89PNG\r\n")
    runner.reconfigure(config2, new_tmdb, h.douban)

    await runner.run_full()
    new_tmdb.search_and_fetch.assert_called_once()

    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        assert item.matched_title == "New Title"


# ---------------------------------------------------------------------------
# M7-T18: Auth batch failure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_auth_batch_failure(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film A (2020)", "Film B (2021)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.side_effect = TmdbAuthError("TMDB API Key 无效")
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.failed == 2  # Both items failed
    assert tmdb.search_and_fetch.call_count == 1  # Only first called, then batch-fail


# ---------------------------------------------------------------------------
# M7-T19: Short transactions (no DB lock across await)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_short_transactions(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    # Make TMDB slow
    event = asyncio.Event()

    async def _slow(title: str, year: object, media_type: str) -> ScrapedMeta:
        # While we're waiting, another session should be able to write
        event.set()
        await asyncio.sleep(0.01)
        return _mock_meta()

    tmdb.search_and_fetch.side_effect = _slow
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # Create a background task that verifies the DB is not locked
    async def _check_db() -> None:
        await event.wait()
        # This should work because the scanner doesn't hold a write txn during await
        with sess:
            count = sess.query(MediaItem).count()
            assert count >= 0

    await asyncio.gather(runner.run_full(), _check_db())


# ---------------------------------------------------------------------------
# M7-T20: Shutdown lifecycle
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shutdown_lifecycle(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "Film (2020)")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    # Start a background task
    tmdb.search_and_fetch = AsyncMock()
    tmdb.search_and_fetch.return_value = _mock_meta()

    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # Run full synchronously, then verify shutdown works
    await runner.run_full()
    await runner.shutdown()

    # After shutdown, new tasks should be rejected
    with pytest.raises(ScanBusyError):
        await runner.run_full()


# ---------------------------------------------------------------------------
# M7-T21: Rescrape manual_needed remains manual_needed on failure
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rescrape_manual_needed_remains_on_failure(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    _make_movie_tree(movies_dir, "1080p.x264-GROUP")  # pure noise

    h = _setup(tmp_path)
    runner = h.runner; 

    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # First scan → manual_needed
    await runner.run_full()

    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        item_id = item.id
        assert item.status == "manual_needed"

    # Rescrape fails because no title to search
    result_item = await runner.rescrape_item(item_id)
    assert result_item.status == "failed"
    assert "标题解析为空" in (result_item.error_message or "")

    # Re-scan: should revert to manual_needed (not stuck in permanent failed loop)
    await runner.run_full()
    with sess:
        item = sess.get(MediaItem, item_id)
        assert item is not None
        assert item.status == "manual_needed"


# ---------------------------------------------------------------------------
# Helper tests
# ---------------------------------------------------------------------------

def test_contains_video_finds_mkv(tmp_path: Path) -> None:
    folder = tmp_path / "test"
    folder.mkdir()
    (folder / "video.mkv").write_text("x")
    assert contains_video(folder) is True


def test_contains_video_empty_dir(tmp_path: Path) -> None:
    folder = tmp_path / "empty"
    folder.mkdir()
    assert contains_video(folder) is False


def test_contains_video_nested(tmp_path: Path) -> None:
    folder = tmp_path / "nested"
    folder.mkdir()
    sub = folder / "sub"
    sub.mkdir()
    (sub / "video.mp4").write_text("x")
    assert contains_video(folder) is True


def test_contains_video_ignores_non_video(tmp_path: Path) -> None:
    folder = tmp_path / "text"
    folder.mkdir()
    (folder / "readme.txt").write_text("x")
    assert contains_video(folder) is False


def test_normalize_path_converts_backslash() -> None:
    result = normalize_path(r"C:\media\movies\Film (2020)")
    assert "\\" not in result
    assert result.startswith("C:/")


def test_normalize_path_strips_trailing() -> None:
    result = normalize_path("/media/movies/Film (2020)/")
    assert not result.endswith("/")


def test_normalize_path_keeps_posix_absolute() -> None:
    # Remote (SSH/WebDAV) paths must stay POSIX-absolute — on Windows they
    # must NOT be rewritten onto the current drive (e.g. D:/Download/...).
    result = normalize_path("/Download/ys_video")
    assert result == "/Download/ys_video"


# ---------------------------------------------------------------------------
# rescrape_item with NFO present (M7-T12 variation)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_rescrape_with_existing_nfo(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    folders = _make_movie_tree(movies_dir, "Film (2020)")
    (folders[0] / "movie.nfo").write_text("<movie/>")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess,"Movies", str(movies_dir), "movie")

    # First scan: NFO exists → skip
    await runner.run_full()
    tmdb.search_and_fetch.assert_not_called()

    # Now force rescrape
    with sess:
        item = sess.query(MediaItem).first()
        assert item is not None
        await runner.rescrape_item(item.id)

    tmdb.search_and_fetch.assert_called_once()


# ---------------------------------------------------------------------------
# Remote library scanning (SSH/WebDAV via Connection abstraction)
# ---------------------------------------------------------------------------

class _FakeConnection:
    """In-memory fake of the Connection interface for remote-library tests."""

    def __init__(self, root: str, files: dict[str, bytes]) -> None:
        self._root = root.rstrip("/")
        self._files = dict(files)  # absolute path -> bytes
        self.written: dict[str, bytes] = {}
        self.close_calls = 0

    @property
    def root(self) -> str:
        return self._root

    def _abs(self, path: str) -> str:
        from pathlib import PurePosixPath
        p = path
        if not p.startswith("/"):
            p = str(PurePosixPath(self._root) / p)
        return p

    async def list_dir(self, path: str) -> list[str]:
        base = self._abs(path)
        names: set[str] = set()
        for ap in list(self._files.keys()):
            if ap.startswith(base + "/"):
                rest = ap[len(base) + 1:]
                top = rest.split("/", 1)[0]
                names.add(top)
        return sorted(names)

    async def is_file(self, path: str) -> bool:
        return self._abs(path) in self._files

    async def is_dir(self, path: str) -> bool:
        base = self._abs(path)
        return any(ap.startswith(base + "/") for ap in self._files)

    async def read_bytes(self, path: str) -> bytes:
        return self._files[self._abs(path)]

    async def write_bytes(self, path: str, data: bytes) -> None:
        ap = self._abs(path)
        self._files[ap] = data
        self.written[ap] = data

    async def mkdir(self, path: str, parents: bool = True) -> None:
        pass

    async def exists(self, path: str) -> bool:
        ap = self._abs(path)
        return ap in self._files or any(k.startswith(ap + "/") for k in self._files)

    async def contains_video(self, folder: str) -> bool:
        from pathlib import PurePosixPath

        from app import VIDEO_EXTENSIONS

        base = self._abs(folder)
        for ap in list(self._files.keys()):
            if (
                PurePosixPath(ap).suffix.lower() in VIDEO_EXTENSIONS
                and ap.startswith(base + "/")
            ):
                return True
        return False

    async def aclose(self) -> None:
        self.close_calls += 1


@pytest.mark.asyncio
async def test_remote_library_scrapes_via_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A remote (ssh) library should scan & write metadata via its Connection."""
    from app import scanner as scanner_mod
    from app.crypto import encrypt_dict, load_or_create_key

    enc_key = load_or_create_key(tmp_path)

    # Fake remote filesystem: a remote root with one movie folder + video file
    remote_root = "/nas/movies"
    files: dict[str, bytes] = {
        remote_root + "/Film (2020)/video.mkv": b"fake",
    }
    fake_conn = _FakeConnection(remote_root, files)

    h = _setup(tmp_path)
    runner = h.runner
    tmdb = h.tmdb

    # Inject enc_key so the runner can decrypt credentials
    runner._enc_key = enc_key  # type: ignore[attr-defined]

    tmdb.search_and_fetch.return_value = _mock_meta(title="Film", source_id="42")

    creds = encrypt_dict(
        {"host": "nas", "port": 22, "username": "u", "password": "p"}, enc_key,
    )
    with h.session() as sess:
        lib = Library(
            name="Remote Movies", path=remote_root, media_type="movie",
            connection_type="ssh", connection_config_encrypted=creds,
        )
        sess.add(lib)
        sess.commit()

    # Patch connection creation to return our fake
    monkeypatch.setattr(
        scanner_mod, "_library_connection",
        lambda lib, key: fake_conn,
    )

    log = await runner.run_full()

    assert log.total == 1
    assert log.matched == 1

    # Metadata files should have been written through the fake connection
    assert (remote_root + "/Film (2020)/movie.nfo") in fake_conn.written
    assert (remote_root + "/Film (2020)/poster.jpg") in fake_conn.written
    # The NFO must contain the matched title
    assert b"Film" in fake_conn.written[remote_root + "/Film (2020)/movie.nfo"]


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def test_library_connection_local_returns_local(tmp_path: Path) -> None:
    from app.connection import LocalConnection
    from app.scanner import _library_connection

    lib = Library(name="L", path="/media/movies", media_type="movie")
    conn = _library_connection(lib, b"irrelevant")
    assert isinstance(conn, LocalConnection)


def test_library_connection_remote_without_key_falls_back(tmp_path: Path) -> None:
    from app.connection import LocalConnection
    from app.scanner import _library_connection

    lib = Library(
        name="L", path="/nas/movies", media_type="movie",
        connection_type="ssh", connection_config_encrypted="garbage",
    )
    # No enc_key → must fall back to LocalConnection, not crash
    conn = _library_connection(lib, None)
    assert isinstance(conn, LocalConnection)


def test_library_connection_remote_with_encrypted_config_creates_webdav(
    tmp_path: Path,
) -> None:
    from app.connection import WebdavConnection
    from app.crypto import encrypt_dict, load_or_create_key
    from app.scanner import _library_connection

    enc_key = load_or_create_key(tmp_path)
    creds = encrypt_dict(
        {"host": "nas", "port": 443, "username": "u", "password": "p"}, enc_key,
    )
    lib = Library(
        name="L", path="/webdav/movies", media_type="movie",
        connection_type="webdav", connection_config_encrypted=creds,
    )
    conn = _library_connection(lib, enc_key)
    assert isinstance(conn, WebdavConnection)


def test_library_connection_remote_with_bad_ciphertext_falls_back(
    tmp_path: Path,
) -> None:
    from app.connection import LocalConnection
    from app.crypto import load_or_create_key
    from app.scanner import _library_connection

    enc_key = load_or_create_key(tmp_path)
    lib = Library(
        name="L", path="/nas/movies", media_type="movie",
        connection_type="ssh", connection_config_encrypted="not-a-valid-token",
    )
    conn = _library_connection(lib, enc_key)
    assert isinstance(conn, LocalConnection)


def test_relative_folder_handles_mixed_separators() -> None:
    from app.scanner import _relative_folder

    # folder normalised to /, library has backslashes → must still resolve
    rel = _relative_folder(
        "C:/m/Film (2020)", r"C:\m",
    )
    assert rel == "Film (2020)"


@pytest.mark.asyncio
async def test_find_video_file_async_depths() -> None:
    from app.scanner import _find_video_file_async

    files = {
        "/root/Film/video.mkv": b"",
        "/root/Show/Season 01/ep01.mp4": b"",
        "/root/Show/readme.txt": b"",
    }
    conn = _FakeConnection("/root", files)

    # Depth-1 video found
    assert await _find_video_file_async(conn, "Film") == "Film/video.mkv"
    # Depth-2 video found
    assert await _find_video_file_async(conn, "Show") == "Show/Season 01/ep01.mp4"


@pytest.mark.asyncio
async def test_nfo_exists_async_via_connection() -> None:
    from app.scanner import _nfo_exists_async

    files = {"/root/Film/movie.nfo": b"<movie/>"}
    conn = _FakeConnection("/root", files)
    assert await _nfo_exists_async(conn, "Film", "movie") is True
    assert await _nfo_exists_async(conn, "Film", "tv") is False  # tvshow.nfo absent


# ---------------------------------------------------------------------------
# Recursive discovery — grouped / deep / loose-file / TV / BDMV
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_grouped_movies(tmp_path: Path) -> None:
    """Genre/Director-grouped layout discovers each movie folder, not the group."""
    movies_dir = tmp_path / "movies"
    for g in range(3):
        genre = movies_dir / f"Genre {g}"
        for m in range(4):
            d = genre / f"Movie {g}-{m} (2020)"
            d.mkdir(parents=True)
            (d / "video.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.total == 12  # 3 genres x 4 movies
    with sess:
        assert sess.query(MediaItem).count() == 12


@pytest.mark.asyncio
async def test_discover_deep_movies(tmp_path: Path) -> None:
    """Videos nested deeper than two levels are still discovered."""
    movies_dir = tmp_path / "movies"
    for i in range(6):
        d = movies_dir / f"Movie {i} (2020)" / "Video" / "x264"
        d.mkdir(parents=True)
        (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.total == 6
    with sess:
        assert sess.query(MediaItem).count() == 6


@pytest.mark.asyncio
async def test_discover_root_loose_files(tmp_path: Path) -> None:
    """Loose video files directly in the library root become file items."""
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    for i in range(5):
        (movies_dir / f"Loose {i} (2020).mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.side_effect = [
        _mock_meta(title=f"Loose {i}", year=2020) for i in range(5)
    ]
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.total == 5
    assert log.matched == 5
    with sess:
        items = sess.query(MediaItem).all()
        assert len(items) == 5
        # folder_path points at the video file itself
        assert all(i.folder_path.endswith(".mkv") for i in items)


@pytest.mark.asyncio
async def test_discover_mixed_root(tmp_path: Path) -> None:
    """Root with both loose files and movie folders counts both."""
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    for i in range(3):
        (movies_dir / f"Loose {i} (2020).mkv").write_text("x")
        d = movies_dir / f"Folder {i} (2020)"
        d.mkdir()
        (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.total == 6
    with sess:
        assert sess.query(MediaItem).count() == 6


@pytest.mark.asyncio
async def test_discover_skips_noise_dirs(tmp_path: Path) -> None:
    """Trailers/Extras/samples must not be counted as movies."""
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    d = movies_dir / "Film (2020)"
    d.mkdir()
    (d / "movie.mkv").write_text("x")
    (movies_dir / "Trailers").mkdir()
    (movies_dir / "Trailers" / "trailer.mkv").write_text("x")
    (movies_dir / "Extras").mkdir()
    (movies_dir / "Extras" / "featurette.mkv").write_text("x")
    (movies_dir / "samples").mkdir()
    (movies_dir / "samples" / "sample.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.total == 1  # only "Film (2020)"
    with sess:
        items = sess.query(MediaItem).all()
        assert len(items) == 1
        assert items[0].parsed_title == "Film"


@pytest.mark.asyncio
async def test_discover_tv_grouped(tmp_path: Path) -> None:
    """Grouped TV (Franchise/Show/Season) yields one item per show, not per season."""
    tv_dir = tmp_path / "tv"
    for s in range(4):
        show = tv_dir / f"Show {s} (2021)" / "Season 01"
        show.mkdir(parents=True)
        (show / "ep01.mkv").write_text("x")
    for f in range(2):
        show = tv_dir / f"Franchise {f}" / f"TvShow {f} (2021)" / "Season 02"
        show.mkdir(parents=True)
        (show / "ep01.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess, "TV", str(tv_dir), "tv")

    log = await runner.run_full()
    assert log.total == 6  # 4 flat shows + 2 grouped shows, NOT +seasons
    with sess:
        assert sess.query(MediaItem).count() == 6


@pytest.mark.asyncio
async def test_discover_bdmv(tmp_path: Path) -> None:
    """A BDMV/VIDEO_TS sub-folder marks its parent as one movie."""
    movies_dir = tmp_path / "movies"
    for i in range(3):
        d = movies_dir / f"Disc {i} (2019)" / "BDMV" / "STREAM"
        d.mkdir(parents=True)
        (d / "x.m2ts").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta(title="Disc")
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.total == 3
    with sess:
        items = sess.query(MediaItem).all()
        assert len(items) == 3
        # Item folder is the disc folder, not the STREAM dir
        assert all(not i.folder_path.endswith("STREAM") for i in items)


# ---------------------------------------------------------------------------
# File-item NFO naming (loose-file items use <video-stem>.nfo)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_file_item_writes_stem_nfo(tmp_path: Path) -> None:
    """A loose-file movie gets <stem>.nfo beside the video, not movie.nfo."""
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    (movies_dir / "Interstellar (2014).mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta(title="Interstellar", year=2014)
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    await runner.run_full()

    assert (movies_dir / "Interstellar (2014).nfo").exists()
    assert not (movies_dir / "movie.nfo").exists()


# ---------------------------------------------------------------------------
# Manual subtitle download (folder + loose-file items)
# ---------------------------------------------------------------------------

def _make_subtitle_runner(tmp_path: Path, files: list[tuple[Path, str]]) -> _RunnerHarness:
    """Build a runner with subtitle enabled and a stubbed downloader."""
    h = _setup(tmp_path, _make_config(subtitle_enabled=True))
    sub = AsyncMock()
    sub.download.return_value = Path("/fake/subtitle.srt")
    sub.aclose = AsyncMock()
    h.runner.set_subtitle_downloader(sub)  # type: ignore[arg-type]
    with h.session() as sess:
        for path, media_type in files:
            _add_library(sess, path.name, str(path), media_type)
    return h


def _add_item(sess: Session, lib: Library, folder_path: str, title: str, year: int) -> int:
    item = MediaItem(
        library_id=lib.id,
        media_type="movie",
        folder_path=folder_path,
        parsed_title=title,
        parsed_year=year,
        status="pending",
    )
    sess.add(item)
    sess.commit()
    return item.id


@pytest.mark.asyncio
async def test_download_subtitle_folder_item(tmp_path: Path) -> None:
    """Manual subtitle for a folder item passes folder + relative video filename."""
    movies_dir = tmp_path / "movies"
    d = movies_dir / "Film (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _make_subtitle_runner(tmp_path, [(movies_dir, "movie")])
    sub = h.runner._subtitle  # type: ignore[attr-defined]

    with h.session() as sess:
        lib = sess.query(Library).one()
        item_id = _add_item(sess, lib, str(movies_dir / "Film (2020)"), "Film", 2020)

    result = await h.runner.download_subtitle(item_id)

    assert result == Path("/fake/subtitle.srt")
    sub.download.assert_awaited_once()
    kwargs = sub.download.await_args.kwargs
    assert kwargs["media_folder"] == Path(str(movies_dir / "Film (2020)"))
    assert kwargs["video_filename"] == "movie.mkv"
    assert kwargs["title"] == "Film"
    assert kwargs["year"] == 2020


@pytest.mark.asyncio
async def test_download_subtitle_file_item(tmp_path: Path) -> None:
    """Manual subtitle for a loose-file item uses the file's parent + name."""
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    (movies_dir / "Loose (2020).mkv").write_text("x")

    h = _make_subtitle_runner(tmp_path, [(movies_dir, "movie")])
    sub = h.runner._subtitle  # type: ignore[attr-defined]

    with h.session() as sess:
        lib = sess.query(Library).one()
        item_id = _add_item(sess, lib, str(movies_dir / "Loose (2020).mkv"), "Loose", 2020)

    result = await h.runner.download_subtitle(item_id)

    assert result == Path("/fake/subtitle.srt")
    sub.download.assert_awaited_once()
    kwargs = sub.download.await_args.kwargs
    assert kwargs["media_folder"] == Path(str(movies_dir))
    assert kwargs["video_filename"] == "Loose (2020).mkv"
    assert kwargs["title"] == "Loose"


@pytest.mark.asyncio
async def test_download_subtitle_disabled_raises(tmp_path: Path) -> None:
    """Subtitle feature disabled → ScrapeError."""
    from app.exceptions import ScrapeError

    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    (movies_dir / "M.mkv").write_text("x")

    h = _setup(tmp_path, _make_config(subtitle_enabled=False))
    h.runner.set_subtitle_downloader(None)
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        item_id = _add_item(sess, lib, str(movies_dir / "M.mkv"), "M", 2020)

    with pytest.raises(ScrapeError):
        await h.runner.download_subtitle(item_id)


@pytest.mark.asyncio
async def test_download_subtitle_item_not_found(tmp_path: Path) -> None:
    """Missing item → ItemNotFoundError."""
    from app.exceptions import ItemNotFoundError

    h = _make_subtitle_runner(tmp_path, [])
    with pytest.raises(ItemNotFoundError):
        await h.runner.download_subtitle(999)


@pytest.mark.asyncio
async def test_download_subtitle_empty_title_raises(tmp_path: Path) -> None:
    """Unparseable title → ScrapeError."""
    from app.exceptions import ScrapeError

    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    (movies_dir / "M.mkv").write_text("x")

    h = _make_subtitle_runner(tmp_path, [(movies_dir, "movie")])
    with h.session() as sess:
        lib = sess.query(Library).one()
        item_id = _add_item(sess, lib, str(movies_dir / "M.mkv"), "", 2020)

    with pytest.raises(ScrapeError):
        await h.runner.download_subtitle(item_id)


@pytest.mark.asyncio
async def test_download_subtitle_prefers_matched_title(tmp_path: Path) -> None:
    """Manual subtitle prefers matched_title/matched_year over parsed values."""
    movies_dir = tmp_path / "movies"
    movies_dir.mkdir()
    (movies_dir / "M.mkv").write_text("x")

    h = _make_subtitle_runner(tmp_path, [(movies_dir, "movie")])
    sub = h.runner._subtitle  # type: ignore[attr-defined]
    with h.session() as sess:
        lib = sess.query(Library).one()
        item_id = _add_item(sess, lib, str(movies_dir / "M.mkv"), "Parsed", 2020)
        item = sess.get(MediaItem, item_id)
        assert item is not None
        item.matched_title = "Matched"
        item.matched_year = 2021
        sess.commit()

    await h.runner.download_subtitle(item_id)

    kwargs = sub.download.await_args.kwargs
    assert kwargs["title"] == "Matched"
    assert kwargs["year"] == 2021


@pytest.mark.asyncio
async def test_download_subtitle_no_video_returns_none(tmp_path: Path) -> None:
    """Folder item with no video file inside → None (no subtitle attempt)."""
    movies_dir = tmp_path / "movies"
    d = movies_dir / "Film (2020)"
    d.mkdir(parents=True)  # empty folder, no video

    h = _make_subtitle_runner(tmp_path, [(movies_dir, "movie")])
    sub = h.runner._subtitle  # type: ignore[attr-defined]
    with h.session() as sess:
        lib = sess.query(Library).one()
        item_id = _add_item(sess, lib, str(movies_dir / "Film (2020)"), "Film", 2020)

    result = await h.runner.download_subtitle(item_id)

    assert result is None
    sub.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_scan_skips_ignored_paths(tmp_path: Path) -> None:
    """Paths in the ignored (deleted) list are not re-added by a scan."""
    from app.database import AppMeta
    from app.scanner import _IGNORED_META_KEY, normalize_path

    movies_dir = tmp_path / "movies"
    d = movies_dir / "Film (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")
    keep = movies_dir / "Keep (2021)"
    keep.mkdir()
    (keep / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")
        sess.add(AppMeta(key=_IGNORED_META_KEY, value=normalize_path(str(d))))
        sess.commit()

    log = await runner.run_full()
    assert log.total == 1
    with sess:
        items = sess.query(MediaItem).all()
        assert len(items) == 1
        assert items[0].parsed_title == "Keep"


@pytest.mark.asyncio
async def test_discover_deep_movie_no_year(tmp_path: Path) -> None:
    """Deep layout without a year in the folder name falls back to the video folder."""
    movies_dir = tmp_path / "movies"
    for i in range(3):
        d = movies_dir / f"Movie {i}" / "Video"
        d.mkdir(parents=True)
        (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    log = await runner.run_full()
    assert log.total == 3
    with sess:
        assert sess.query(MediaItem).count() == 3


# ---------------------------------------------------------------------------
# Manual stop (concurrency)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stop_background_scan(tmp_path: Path) -> None:
    """stop() cancels the running scan and marks remaining items as stopped."""
    movies_dir = tmp_path / "movies"
    for i in range(3):
        d = movies_dir / f"Movie {i} (2020)"
        d.mkdir(parents=True)
        (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    entered = asyncio.Event()

    async def hang_after_entering(*args: object, **kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    tmdb.search_and_fetch.side_effect = hang_after_entering
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    task = runner.start_full_background()
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    assert runner.is_running

    assert runner.stop() is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    assert runner.is_running is False

    with h.session() as sess:
        items = sess.query(MediaItem).all()
        assert len(items) == 3
        assert all(i.status == "failed" for i in items)
        assert all("任务已手动停止" in (i.error_message or "") for i in items)


@pytest.mark.asyncio
async def test_stop_when_idle_returns_false(tmp_path: Path) -> None:
    h = _setup(tmp_path)
    assert h.runner.stop() is False


@pytest.mark.asyncio
async def test_scan_can_restart_after_stop(tmp_path: Path) -> None:
    """After a stop, the mutex is released and a new scan runs normally."""
    movies_dir = tmp_path / "movies"
    d = movies_dir / "Movie (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    entered = asyncio.Event()

    async def hang_after_entering(*args: object, **kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    tmdb.search_and_fetch.side_effect = hang_after_entering
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    task = runner.start_full_background()
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    assert runner.is_running
    assert runner.stop() is True
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5.0)
    assert runner.is_running is False

    # A fresh scan is allowed again
    tmdb.search_and_fetch.side_effect = None
    tmdb.search_and_fetch.return_value = _mock_meta()
    log = await runner.run_full()
    assert log.total == 1
    with h.session() as sess:
        assert sess.query(MediaItem).one().status == "matched"


@pytest.mark.asyncio
async def test_stop_awaited_run_full(tmp_path: Path) -> None:
    """stop() also cancels a run_full() awaited by the scheduler."""
    movies_dir = tmp_path / "movies"
    d = movies_dir / "Movie (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    entered = asyncio.Event()

    async def hang_after_entering(*args: object, **kwargs: object) -> None:
        entered.set()
        await asyncio.Event().wait()

    tmdb.search_and_fetch.side_effect = hang_after_entering
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    task = asyncio.create_task(runner.run_full())  # mirrors the scheduler path
    await asyncio.wait_for(entered.wait(), timeout=5.0)
    assert runner.is_running
    assert runner.stop() is True

    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.is_running is False
    with h.session() as sess:
        item = sess.query(MediaItem).one()
        assert item.status == "failed"
        assert "任务已手动停止" in (item.error_message or "")


@pytest.mark.asyncio
async def test_rescrape_reparses_folder_name(tmp_path: Path) -> None:
    """Manual rescrape re-parses the folder name instead of the stale stored title."""
    movies_dir = tmp_path / "movies"
    folder = "12.蚁人1(2015).Ant Man 2015 UHD BluRay REMUX 2160p HEVC Atmos TrueHD 7.1-PTer"
    d = movies_dir / folder
    d.mkdir(parents=True)
    (d / "video.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta(title="蚁人")
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        # Simulate a stale parsed_title stored by an older parser version
        item_id = _add_item(sess, lib, str(d), "12 蚁人1", 2015)

    await runner.rescrape_item(item_id)

    # Parser now extracts the English title from the post-bracket region
    # ("Ant Man") since it matches TMDB better than the CJK title.
    assert tmdb.search_and_fetch.await_args.args[0] == "Ant Man"


# ---------------------------------------------------------------------------
# imdb_id storage + subtitle search uses matched data
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_scrape_stores_imdb_id(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    d = movies_dir / "Film (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta(imdb_id="tt1234567")
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    await runner.run_full()

    with h.session() as sess:
        item = sess.query(MediaItem).one()
        assert item.imdb_id == "tt1234567"


@pytest.mark.asyncio
async def test_auto_subtitle_uses_imdb_and_original_title(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    d = movies_dir / "Film (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path, _make_config(subtitle_enabled=True))
    runner = h.runner; tmdb = h.tmdb
    sub = AsyncMock()
    sub.download.return_value = Path("/fake.srt")
    sub.aclose = AsyncMock()
    runner.set_subtitle_downloader(sub)  # type: ignore[arg-type]
    tmdb.search_and_fetch.return_value = _mock_meta(original_title="Original Film", imdb_id="tt99")
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    await runner.run_full()

    kwargs = sub.download.await_args.kwargs
    assert kwargs["title"] == "Original Film"
    assert kwargs["year"] == 2020
    assert kwargs["imdb_id"] == "tt99"


@pytest.mark.asyncio
async def test_manual_subtitle_writes_log(tmp_path: Path) -> None:
    from app.database import ScrapeLog

    movies_dir = tmp_path / "movies"
    d = movies_dir / "Film (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _make_subtitle_runner(tmp_path, [(movies_dir, "movie")])
    with h.session() as sess:
        lib = sess.query(Library).one()
        item_id = _add_item(sess, lib, str(d), "Film", 2020)

    await h.runner.download_subtitle(item_id)

    with h.session() as sess:
        logs = sess.query(ScrapeLog).all()
        assert len(logs) == 1
        assert logs[0].matched == 1
        assert "手动字幕" in (logs[0].detail or "")


@pytest.mark.asyncio
async def test_manual_subtitle_not_found_log(tmp_path: Path) -> None:
    from app.database import ScrapeLog

    movies_dir = tmp_path / "movies"
    d = movies_dir / "Film (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _make_subtitle_runner(tmp_path, [(movies_dir, "movie")])
    sub = h.runner._subtitle  # type: ignore[attr-defined]
    sub.download.return_value = None  # no match
    with h.session() as sess:
        lib = sess.query(Library).one()
        item_id = _add_item(sess, lib, str(d), "Film", 2020)

    result = await h.runner.download_subtitle(item_id)

    assert result is None
    with h.session() as sess:
        log = sess.query(ScrapeLog).one()
        assert log.matched == 0
        assert "未找到可用字幕" in (log.detail or "")


@pytest.mark.asyncio
async def test_discover_tv_episode_subfolders(tmp_path: Path) -> None:
    """A show whose episodes live one-per-folder yields one item, not per-episode."""
    tv_dir = tmp_path / "tv"
    show = tv_dir / "黑镜 Black Mirror[全7季]"
    for i in range(3):
        d = show / f"第{i + 1}集（201{6 + i}）"
        d.mkdir(parents=True)
        (d / "ep.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta(title="黑镜")
    with h.session() as sess:
        _add_library(sess, "TV", str(tv_dir), "tv")

    log = await runner.run_full()
    assert log.total == 1
    with h.session() as sess:
        items = sess.query(MediaItem).all()
        assert len(items) == 1
        assert items[0].parsed_title == "黑镜 Black Mirror"


@pytest.mark.asyncio
async def test_rescrape_failed_reprocesses_only_failed(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    d1 = movies_dir / "Good (2020)"
    d1.mkdir(parents=True)
    (d1 / "movie.mkv").write_text("x")
    d2 = movies_dir / "Bad (2021)"
    d2.mkdir(parents=True)
    (d2 / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta(title="Matched")
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        good_id = _add_item(sess, lib, str(d1), "Good", 2020)
        bad_id = _add_item(sess, lib, str(d2), "Bad", 2021)
        sess.get(MediaItem, good_id).status = "matched"
        sess.get(MediaItem, bad_id).status = "failed"
        sess.commit()

    tmdb.search_and_fetch.reset_mock()
    log = await runner.rescrape_failed()

    assert log.total == 1
    assert log.matched == 1
    # Only the failed item was re-scraped
    assert tmdb.search_and_fetch.await_args.args[0] == "Bad"
    with h.session() as sess:
        assert sess.get(MediaItem, good_id).status == "matched"
        assert sess.get(MediaItem, bad_id).status == "matched"


@pytest.mark.asyncio
async def test_rescrape_failed_no_failed_items(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    d1 = movies_dir / "Good (2020)"
    d1.mkdir(parents=True)
    (d1 / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta()
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        _add_item(sess, lib, str(d1), "Good", 2020)

    log = await runner.rescrape_failed()

    assert log.total == 0
    assert log.matched == 0
    tmdb.search_and_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_rescrape_item_with_query_override(tmp_path: Path) -> None:
    """A manual query overrides the (bad) parsed title for the search."""
    movies_dir = tmp_path / "movies"
    d = movies_dir / "Bad Name (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb
    tmdb.search_and_fetch.return_value = _mock_meta(title="Correct")
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        item_id = _add_item(sess, lib, str(d), "Bad Name", 2020)

    await runner.rescrape_item(item_id, query="Correct Title")

    assert tmdb.search_and_fetch.await_args.args[0] == "Correct Title"


@pytest.mark.asyncio
async def test_rescrape_item_with_tmdb_id(tmp_path: Path) -> None:
    """A manual tmdb_id forces fetching that specific id (no search)."""
    from app.scrapers.base import ScrapedMeta

    movies_dir = tmp_path / "movies"
    d = movies_dir / "Bad (2020)"
    d.mkdir(parents=True)
    (d / "movie.mkv").write_text("x")

    h = _setup(tmp_path)
    runner = h.runner; tmdb = h.tmdb

    async def _fake_fetch_by_id(tid: int, media_type: str) -> ScrapedMeta:
        return ScrapedMeta(
            source="tmdb", source_id=str(tid), title="Matched",
            original_title="Matched", year=2020, overview="", rating=0, genres=[],
        )

    tmdb.fetch_by_id = AsyncMock(side_effect=_fake_fetch_by_id)
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        item_id = _add_item(sess, lib, str(d), "Bad", 2020)

    await runner.rescrape_item(item_id, tmdb_id=42)

    assert tmdb.fetch_by_id.await_args.args[0] == 42
    tmdb.search_and_fetch.assert_not_awaited()  # forced id bypasses search


# ---------------------------------------------------------------------------
# Subtitle-status detection (display)
# ---------------------------------------------------------------------------
def test_detect_subtitle_summary() -> None:
    from app.scanner import detect_subtitle_summary

    assert detect_subtitle_summary(["movie.mkv", "poster.jpg"]) is None
    assert detect_subtitle_summary(["movie.zh-Hans.srt"]) == "简体中文"
    assert detect_subtitle_summary(["movie.chs.ass"]) == "简体中文"
    assert detect_subtitle_summary(["movie.简体中文.srt"]) == "简体中文"
    assert detect_subtitle_summary(["movie.zh-Hant.srt"]) == "繁体中文"
    assert detect_subtitle_summary(["movie.zh.srt"]) == "中文"
    assert detect_subtitle_summary(["movie.eng.srt"]) == "有字幕(非中文)"
    assert detect_subtitle_summary(["movie.srt"]) == "有字幕(非中文)"


# ---------------------------------------------------------------------------
# Hardening: per-library rescan state machine
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_rescan_library_rediscovers_and_scrapes_items(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    folder = movies_dir / "Film (2020)"
    folder.mkdir(parents=True)
    (folder / "video.mkv").write_bytes(b"video")

    h = _setup(tmp_path)
    h.tmdb.search_and_fetch.return_value = _mock_meta(
        title="Film", poster_url=None, backdrop_url=None,
    )
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        lib_id = lib.id

    log = await h.runner.rescan_library(lib_id)

    assert (log.total, log.matched, log.failed) == (1, 1, 0)
    assert "发现 1 个条目" in (log.detail or "")
    with h.session() as sess:
        item = sess.query(MediaItem).one()
        assert item.status == "matched"
        assert item.matched_title == "Film"
    assert (folder / "movie.nfo").exists()


@pytest.mark.asyncio
async def test_rescan_library_loads_existing_nfo_metadata(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    folder = movies_dir / "Bundled (2020)"
    folder.mkdir(parents=True)
    (folder / "video.mkv").write_bytes(b"video")
    (folder / "movie.nfo").write_text(
        "<movie><title>Bundled Title</title><originaltitle>Original</originaltitle>"
        "<year>2020</year><rating>8.5</rating><plot>Plot</plot>"
        "<genre>Drama</genre><uniqueid type='tmdb'>42</uniqueid>"
        "<uniqueid type='imdb'>tt42</uniqueid></movie>",
        encoding="utf-8",
    )

    h = _setup(tmp_path)
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        lib_id = lib.id

    log = await h.runner.rescan_library(lib_id)

    assert (log.total, log.matched, log.failed) == (1, 0, 0)
    with h.session() as sess:
        item = sess.query(MediaItem).one()
        assert item.status == "matched"
        assert item.source == "nfo"
        assert item.matched_title == "Bundled Title"
        assert item.genres == "Drama"
        assert item.imdb_id == "tt42"
    h.tmdb.search_and_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_rescan_library_records_inaccessible_connection(tmp_path: Path) -> None:
    h = _setup(tmp_path)
    missing_root = tmp_path / "missing"
    with h.session() as sess:
        lib = _add_library(sess, "Offline", str(missing_root), "movie")
        lib_id = lib.id

    log = await h.runner.rescan_library(lib_id)

    assert (log.total, log.matched, log.failed) == (0, 0, 1)
    assert f"库重新扫描失败: {missing_root}" in (log.detail or "")
    assert log.finished_at is not None


@pytest.mark.asyncio
async def test_rescan_library_missing_id_finalizes_log(tmp_path: Path) -> None:
    from app.exceptions import ItemNotFoundError

    h = _setup(tmp_path)
    with pytest.raises(ItemNotFoundError, match="Library 999"):
        await h.runner.rescan_library(999)

    assert h.runner.is_running is False
    with h.session() as sess:
        log = sess.query(ScrapeLog).one()
        assert log.finished_at is not None
        assert (log.total, log.matched, log.failed) == (0, 0, 0)


@pytest.mark.asyncio
async def test_rescan_library_isolates_item_scrape_failure(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    folder = movies_dir / "Broken (2020)"
    folder.mkdir(parents=True)
    (folder / "video.mkv").write_bytes(b"video")

    h = _setup(tmp_path)
    h.tmdb.search_and_fetch.side_effect = RuntimeError("TMDB unavailable")
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        lib_id = lib.id

    log = await h.runner.rescan_library(lib_id)

    assert (log.total, log.matched, log.failed) == (1, 0, 1)
    assert "TMDB unavailable" in (log.detail or "")
    with h.session() as sess:
        item = sess.query(MediaItem).one()
        assert item.status == "failed"
        assert item.error_message == "TMDB unavailable"


@pytest.mark.asyncio
async def test_background_rescan_rejects_busy_start(tmp_path: Path) -> None:
    h = _setup(tmp_path)
    release = asyncio.Event()

    async def blocked(lib_id: int) -> ScrapeLog:
        await release.wait()
        return ScrapeLog(total=lib_id, matched=0, failed=0)

    h.runner._rescan_library_impl = blocked  # type: ignore[method-assign]
    task = h.runner.start_rescan_library_background(1)
    with pytest.raises(ScanBusyError):
        h.runner.start_rescan_library_background(2)

    release.set()
    assert (await task).total == 1
    assert h.runner._current_task is None


# ---------------------------------------------------------------------------
# Hardening: subtitle refresh branches and path hand-off
# ---------------------------------------------------------------------------

def _add_matched_item(
    sess: Session,
    lib: Library,
    folder_path: str,
    title: str,
    *,
    original_title: str | None = None,
) -> int:
    item = MediaItem(
        library_id=lib.id,
        media_type=lib.media_type,
        folder_path=folder_path,
        parsed_title=title,
        parsed_year=2020,
        matched_title=title,
        matched_original_title=original_title,
        matched_year=2020,
        status="matched",
    )
    sess.add(item)
    sess.commit()
    return item.id


@pytest.mark.asyncio
async def test_refresh_subtitles_rejects_disabled_downloader(tmp_path: Path) -> None:
    from app.exceptions import ScrapeError

    h = _setup(tmp_path, _make_config(subtitle_enabled=False))

    with pytest.raises(ScrapeError, match="字幕功能未启用"):
        await h.runner._refresh_subtitles_impl()


@pytest.mark.asyncio
async def test_refresh_subtitles_skips_empty_and_chinese_titles(tmp_path: Path) -> None:
    h = _setup(tmp_path, _make_config(subtitle_enabled=True))
    subtitle = AsyncMock()
    h.runner.set_subtitle_downloader(subtitle)  # type: ignore[arg-type]
    with h.session() as sess:
        lib = _add_library(sess, "Remote", "/movies", "movie")
        _add_matched_item(sess, lib, "/movies/Empty", "")
        _add_matched_item(sess, lib, "/movies/Chinese", "流浪地球")

    log = await h.runner._refresh_subtitles_impl()

    assert (log.total, log.matched, log.failed) == (0, 0, 0)
    assert log.detail is None
    subtitle.download.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_subtitles_isolates_results_and_closes_connections(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import scanner as scanner_mod

    h = _setup(tmp_path, _make_config(subtitle_enabled=True))
    subtitle = AsyncMock()

    async def download(**kwargs: object) -> Path:
        if kwargs["title"] == "Error":
            raise RuntimeError("provider failed")
        return Path("/saved/video.zh.srt")

    subtitle.download.side_effect = download
    h.runner.set_subtitle_downloader(subtitle)  # type: ignore[arg-type]

    with h.session() as sess:
        lib = _add_library(sess, "Remote", "/movies", "movie")
        _add_matched_item(sess, lib, "/movies/Success", "Success")
        _add_matched_item(sess, lib, "/movies/NoVideo", "No Video")
        _add_matched_item(sess, lib, "/movies/Error", "Error")

    files = {
        "/movies/Success/video.mkv": b"video",
        "/movies/Error/video.mkv": b"video",
    }
    connections: list[_FakeConnection] = []

    def connection_for_target(*args: object) -> _FakeConnection:
        conn = _FakeConnection("/movies", files)
        connections.append(conn)
        return conn

    monkeypatch.setattr(scanner_mod, "_library_connection_from_target", connection_for_target)

    log = await h.runner._refresh_subtitles_impl()

    assert (log.total, log.matched, log.failed) == (3, 1, 2)
    assert "字幕已下载: /movies/Success" in (log.detail or "")
    assert "未找到字幕: /movies/NoVideo" in (log.detail or "")
    assert "/movies/Error: provider failed" in (log.detail or "")
    assert len(connections) == 3
    assert all(conn.close_calls == 1 for conn in connections)
    assert subtitle.download.await_count == 2


@pytest.mark.asyncio
async def test_refresh_subtitles_cancellation_finalizes_log_and_closes_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import scanner as scanner_mod

    h = _setup(tmp_path, _make_config(subtitle_enabled=True))
    entered = asyncio.Event()
    subtitle = AsyncMock()

    async def blocked_download(**kwargs: object) -> Path:
        entered.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    subtitle.download.side_effect = blocked_download
    h.runner.set_subtitle_downloader(subtitle)  # type: ignore[arg-type]
    with h.session() as sess:
        lib = _add_library(sess, "Remote", "/movies", "movie")
        _add_matched_item(sess, lib, "/movies/Film", "Film")

    conn = _FakeConnection("/movies", {"/movies/Film/video.mkv": b"video"})
    monkeypatch.setattr(
        scanner_mod,
        "_library_connection_from_target",
        lambda *args: conn,
    )

    task = h.runner.start_refresh_subtitles_background()
    await entered.wait()
    assert h.runner.stop() is True
    with pytest.raises(asyncio.CancelledError):
        await task

    assert conn.close_calls == 1
    assert h.runner.is_running is False
    with h.session() as sess:
        log = sess.query(ScrapeLog).one()
        assert log.finished_at is not None
        assert (log.total, log.matched, log.failed) == (1, 0, 1)
        assert "任务已手动停止" in (log.detail or "")


@pytest.mark.asyncio
async def test_subtitle_target_preserves_nested_remote_video_path(tmp_path: Path) -> None:
    from app.scanner import ScrapeTarget

    h = _setup(tmp_path, _make_config(subtitle_enabled=True))
    subtitle = AsyncMock()
    subtitle.download.return_value = Path("/nas/movies/Film/Disc/video.zh.ass")
    h.runner.set_subtitle_downloader(subtitle)  # type: ignore[arg-type]
    target = ScrapeTarget(
        id=1,
        library_id=1,
        library_path="/nas/movies",
        connection_type="ssh",
        connection_config_encrypted=None,
        folder_path="/nas/movies/Film",
        media_type="movie",
        parsed_title="Film",
        parsed_year=2020,
        status="matched",
    )
    conn = _FakeConnection(
        "/nas/movies",
        {"/nas/movies/Film/Disc/video.mkv": b"video"},
    )

    result = await h.runner._download_subtitle_for_target(
        target,
        conn,  # type: ignore[arg-type]
        title="Film",
        year=2020,
    )

    assert result == Path("/nas/movies/Film/Disc/video.zh.ass")
    kwargs = subtitle.download.await_args.kwargs
    assert kwargs["media_folder"].as_posix() == "/nas/movies/Film"
    assert kwargs["video_filename"] == "Disc/video.mkv"
    assert kwargs["connection"] is conn


# ---------------------------------------------------------------------------
# Hardening: helpers and malformed dynamic values
# ---------------------------------------------------------------------------

def test_apply_nfo_meta_ignores_bad_numbers_and_non_iterable_genres() -> None:
    from datetime import UTC, datetime

    from app.scanner import _apply_nfo_meta

    item = MediaItem(
        library_id=1,
        media_type="movie",
        folder_path="/movies/Film",
        status="pending",
    )
    now = datetime.now(UTC)

    _apply_nfo_meta(
        item,
        {
            "title": "Film",
            "year": "not-a-year",
            "rating": object(),
            "genres": 42,
            "plot": "Plot",
        },
        now,
    )

    assert item.matched_title == "Film"
    assert item.matched_year is None
    assert item.rating is None
    assert item.genres is None
    assert item.overview == "Plot"
    assert item.status == "matched"
    assert item.last_scraped_at == now


def test_ignored_paths_round_trip_is_sorted_and_filters_blanks(tmp_path: Path) -> None:
    from app.scanner import _ignored_paths, _set_ignored_paths

    h = _setup(tmp_path)
    with h.factory.begin() as sess:
        _set_ignored_paths(sess, {"/z", "/a"})
    with h.session() as sess:
        meta = sess.get(AppMeta, "ignored_paths")
        assert meta is not None
        assert meta.value == "/a\n/z"
        meta.value += "\n\n"
        sess.commit()
    with h.session() as sess:
        assert _ignored_paths(sess) == {"/a", "/z"}


def test_relative_folder_rejects_path_outside_library() -> None:
    from app.scanner import _relative_folder

    with pytest.raises(ValueError):
        _relative_folder("/elsewhere/Film", "/movies")


def test_library_connection_invalid_encrypted_port_uses_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import scanner as scanner_mod
    from app.crypto import encrypt_dict, load_or_create_key

    key = load_or_create_key(tmp_path)
    encrypted = encrypt_dict(
        {"host": "nas", "port": "not-a-port", "username": "u", "password": "p"},
        key,
    )
    lib = Library(
        name="Remote",
        path="/movies",
        media_type="movie",
        connection_type="ssh",
        connection_config_encrypted=encrypted,
    )
    sentinel = MagicMock()
    captured: list[object] = []

    def fake_create(config: object, root: str) -> object:
        captured.append(config)
        assert root == "/movies"
        return sentinel

    monkeypatch.setattr(scanner_mod, "create_connection", fake_create)

    assert scanner_mod._library_connection(lib, key) is sentinel
    assert captured[0].port == 0  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_deep_movie_connection_error_returns_false() -> None:
    from app.scanner import _looks_like_deep_movie

    conn = AsyncMock()
    conn.contains_video.side_effect = OSError("offline")

    assert await _looks_like_deep_movie(
        conn,  # type: ignore[arg-type]
        "Film (2020)",
        ["Disc"],
    ) is False


def test_persist_result_handles_missing_and_existing_nfo_marker(tmp_path: Path) -> None:
    from app.scanner import ExistingNfoMatched, _persist_result

    h = _setup(tmp_path)
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(tmp_path / "movies"), "movie")
        item_id = _add_item(sess, lib, str(tmp_path / "movies" / "Film"), "Film", 2020)
        item = sess.get(MediaItem, item_id)
        assert item is not None
        item.status = "failed"
        item.error_message = "old error"
        sess.commit()

    with h.factory.begin() as sess:
        _persist_result(sess, 999, ExistingNfoMatched())
        _persist_result(sess, item_id, ExistingNfoMatched())

    with h.session() as sess:
        item = sess.get(MediaItem, item_id)
        assert item is not None
        assert item.status == "matched"
        assert item.error_message is None


def test_scanner_filesystem_helpers_tolerate_io_errors() -> None:
    from app.scanner import find_video_file

    folder = MagicMock(spec=Path)
    folder.iterdir.side_effect = OSError("offline")

    assert find_video_file(folder) is None  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_connection_helpers_return_safe_results_after_io_errors() -> None:
    from app.scanner import (
        _detect_subtitle_summary_async,
        _find_video_file_async,
        _nfo_exists_async,
        _read_nfo_meta_async,
    )

    conn = AsyncMock()
    conn.exists.side_effect = OSError("offline")
    conn.read_bytes.side_effect = RuntimeError("bad NFO")
    assert await _nfo_exists_async(conn, "Film", "movie") is False
    assert await _read_nfo_meta_async(conn, "Film", "movie") is None

    conn.reset_mock()
    conn.list_dir.side_effect = OSError("offline")
    assert await _find_video_file_async(conn, "Film") is None
    assert await _detect_subtitle_summary_async(conn, "Film/video.mkv") is None
    conn.list_dir.assert_awaited_with("Film")


@pytest.mark.asyncio
async def test_find_video_file_skips_broken_entry_and_keeps_searching() -> None:
    from app.scanner import _find_video_file_async

    conn = AsyncMock()
    conn.list_dir.return_value = ["broken", "video.mkv"]
    conn.is_file.side_effect = [OSError("bad entry"), True]

    assert await _find_video_file_async(conn, "Film") == "Film/video.mkv"


@pytest.mark.asyncio
async def test_discovery_skips_entry_stat_error() -> None:
    from app.scanner import _discover_folders

    conn = AsyncMock()
    conn.list_dir.return_value = ["broken.mkv"]
    conn.is_file.side_effect = OSError("bad stat")
    lib = Library(path="/movies", media_type="movie")

    assert await _discover_folders(conn, lib) == set()


def test_subtitle_and_episode_classification_edge_cases() -> None:
    from app.scanner import _looks_like_episode_container, detect_subtitle_summary

    assert detect_subtitle_summary(["电影.繁体中文.srt"]) == "繁体中文"
    assert detect_subtitle_summary(["电影.srt"]) == "中文"
    assert _looks_like_episode_container("Show", []) is False
    assert _looks_like_episode_container("1080p", ["Extras"]) is False
    assert _looks_like_episode_container("Show", ["Bonus S01 material"]) is True


@pytest.mark.asyncio
async def test_rescan_library_marks_unparseable_existing_nfo_without_tmdb(
    tmp_path: Path,
) -> None:
    movies_dir = tmp_path / "movies"
    folder = movies_dir / "Bundled (2020)"
    folder.mkdir(parents=True)
    (folder / "video.mkv").write_bytes(b"video")
    (folder / "movie.nfo").write_text("<movie/>", encoding="utf-8")

    h = _setup(tmp_path)
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        lib_id = lib.id

    log = await h.runner.rescan_library(lib_id)

    assert (log.total, log.matched, log.failed) == (1, 1, 0)
    with h.session() as sess:
        item = sess.query(MediaItem).one()
        assert item.status == "matched"
        assert item.error_message is None
    h.tmdb.search_and_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_manual_subtitle_failure_is_logged_and_connection_released(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    folder = movies_dir / "Film (2020)"
    folder.mkdir(parents=True)
    (folder / "video.mkv").write_bytes(b"video")

    h = _make_subtitle_runner(tmp_path, [(movies_dir, "movie")])
    subtitle = h.runner._subtitle
    assert subtitle is not None
    subtitle.download.side_effect = RuntimeError("provider offline")  # type: ignore[attr-defined]
    with h.session() as sess:
        lib = sess.query(Library).one()
        item_id = _add_item(sess, lib, str(folder), "Film", 2020)

    with pytest.raises(RuntimeError, match="provider offline"):
        await h.runner.download_subtitle(item_id)

    assert h.runner.is_running is False
    with h.session() as sess:
        log = sess.query(ScrapeLog).one()
        assert (log.total, log.matched, log.failed) == (1, 0, 1)
        assert log.finished_at is not None
        assert "provider offline" in (log.detail or "")


@pytest.mark.asyncio
async def test_background_rescrape_failed_stops_after_auth_error(tmp_path: Path) -> None:
    movies_dir = tmp_path / "movies"
    first = movies_dir / "First (2020)"
    second = movies_dir / "Second (2021)"
    for folder in (first, second):
        folder.mkdir(parents=True)
        (folder / "video.mkv").write_bytes(b"video")

    h = _setup(tmp_path)
    h.tmdb.search_and_fetch.side_effect = TmdbAuthError("invalid key")
    with h.session() as sess:
        lib = _add_library(sess, "Movies", str(movies_dir), "movie")
        ids = [
            _add_item(sess, lib, str(first), "First", 2020),
            _add_item(sess, lib, str(second), "Second", 2021),
        ]
        for item_id in ids:
            item = sess.get(MediaItem, item_id)
            assert item is not None
            item.status = "failed"
        sess.commit()

    log = await h.runner.start_rescrape_failed_background()

    assert (log.total, log.matched, log.failed) == (2, 0, 2)
    assert "invalid key" in (log.detail or "")
    assert h.tmdb.search_and_fetch.await_count == 1
    with h.session() as sess:
        items = sess.query(MediaItem).order_by(MediaItem.id).all()
        assert all(item.status == "failed" for item in items)
        assert all(item.error_message == "invalid key" for item in items)
