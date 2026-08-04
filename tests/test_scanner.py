"""M7 scanner tests — M7-T1 through M7-T21."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.orm import Session

from app.config import AppConfig
from app.database import (
    Library,
    MediaItem,
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
        pass


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

async def _hang_search(*args: object, **kwargs: object) -> None:
    await asyncio.Event().wait()  # never completes → cancellable


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
    tmdb.search_and_fetch.side_effect = _hang_search
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    task = runner.start_full_background()
    await asyncio.sleep(0.05)
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
    tmdb.search_and_fetch.side_effect = _hang_search
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    task = runner.start_full_background()
    await asyncio.sleep(0.05)
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
    tmdb.search_and_fetch.side_effect = _hang_search
    with h.session() as sess:
        _add_library(sess, "Movies", str(movies_dir), "movie")

    task = asyncio.create_task(runner.run_full())  # mirrors the scheduler path
    await asyncio.sleep(0.05)
    assert runner.is_running
    assert runner.stop() is True

    with pytest.raises(asyncio.CancelledError):
        await task
    assert runner.is_running is False
    with h.session() as sess:
        item = sess.query(MediaItem).one()
        assert item.status == "failed"
        assert "任务已手动停止" in (item.error_message or "")
