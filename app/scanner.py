"""Scan and scrape orchestration (M7).

Core module that ties together directory scanning, database sync, NFO
detection, TMDB/Douban scraping, image download, and NFO writing.

Implements the definitive state machine from implementation spec §9.1.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app import VIDEO_EXTENSIONS
from app.config import AppConfig
from app.database import Library, MediaItem, ScrapeLog
from app.exceptions import (
    ItemNotFoundError,
    ScanBusyError,
    ScrapeError,
    TmdbAuthError,
)
from app.nfo_writer import nfo_exists, write_movie_nfo, write_tvshow_nfo
from app.parsers.filename_parser import ParsedName, parse_folder_name
from app.scrapers.base import ScrapedMeta
from app.scrapers.douban import DoubanScraper
from app.scrapers.subtitle import SubtitleDownloader
from app.scrapers.tmdb import TmdbScraper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScrapeTarget:
    """Immutable DTO passed from DB read to network scrape."""

    id: int
    folder_path: Path
    media_type: str
    parsed_title: str | None
    parsed_year: int | None
    status: str


class ExistingNfoMatched:
    """Marker: item was matched because a valid NFO already exists."""


ScrapeResult = ScrapedMeta | ExistingNfoMatched


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def contains_video(folder: Path) -> bool:
    """Check if *folder* contains at least one video file (max depth 2)."""
    try:
        for child in folder.iterdir():
            if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                return True
            if child.is_dir():
                for sub in child.iterdir():
                    if sub.is_file() and sub.suffix.lower() in VIDEO_EXTENSIONS:
                        return True
    except OSError:
        pass
    return False


def normalize_path(path: str) -> str:
    """Normalise a path to an absolute POSIX string.

    Uses ``os.path.normpath`` — does **not** resolve symlinks.
    """
    norm = os.path.normpath(os.path.abspath(path))
    return norm.replace("\\", "/")


# ---------------------------------------------------------------------------
# ScanRunner
# ---------------------------------------------------------------------------


class ScanRunner:
    """Orchestrates media scanning and metadata scraping.

    All public mutation methods acquire an internal mutex before any ``await``
    to guarantee single-task execution (process-local).
    """

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        config: AppConfig,
        tmdb: TmdbScraper,
        douban: DoubanScraper | None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._tmdb = tmdb
        self._douban = douban
        self._subtitle: SubtitleDownloader | None = None
        self._running = False
        self._accepting = True
        self._current_task: asyncio.Task[object] | None = None

    # ------------------------------------------------------------------
    # Concurrency control
    # ------------------------------------------------------------------

    def _claim(self) -> None:
        """Acquire the internal mutex.  Must be called **before** any await."""
        if not self._accepting:
            raise ScanBusyError("Scanner is shutting down")
        if self._running:
            raise ScanBusyError("任务正在运行中，请稍后")
        self._running = True
        self._current_task = asyncio.current_task()

    def _release(self) -> None:
        """Release the internal mutex."""
        if self._current_task is asyncio.current_task():
            self._current_task = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    # ------------------------------------------------------------------
    # Configuration hot-reload
    # ------------------------------------------------------------------

    def set_subtitle_downloader(self, downloader: SubtitleDownloader | None) -> None:
        """Configure the subtitle downloader (created after runner init)."""
        self._subtitle = downloader

    def reconfigure(
        self,
        config: AppConfig,
        tmdb: TmdbScraper,
        douban: DoubanScraper | None,
    ) -> tuple[TmdbScraper, DoubanScraper | None]:
        """Synchronously swap in new config and scrapers; return old ones."""
        if self._running:
            raise ScanBusyError("任务运行中不能修改配置")
        old_tmdb = self._tmdb
        old_douban = self._douban
        self._config = config
        self._tmdb = tmdb
        self._douban = douban
        return old_tmdb, old_douban

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    async def run_full(self) -> ScrapeLog:
        """Run a full scan+scrape cycle (used by both scheduler and manual trigger)."""
        self._claim()
        try:
            return await self._run_full_impl()
        finally:
            self._release()

    def start_full_background(self) -> asyncio.Task[ScrapeLog]:
        """Start a full scan in the background; returns the Task immediately."""
        self._claim()
        try:
            task = asyncio.create_task(self._run_full_impl())
            self._current_task = task

            def _done(t: asyncio.Task[object]) -> None:
                self._release()
                if not t.cancelled():
                    exc = t.exception()
                    if exc is not None:
                        logger.error("Background scan failed: %s", exc)

            task.add_done_callback(_done)
            return task
        except Exception:
            self._release()
            raise

    async def rescrape_item(self, item_id: int) -> MediaItem:
        """Force re-scrape a single item (ignores existing NFO)."""
        self._claim()
        try:
            return await self._rescrape_item_impl(item_id)
        finally:
            self._release()

    async def shutdown(self) -> None:
        """Graceful shutdown: reject new work, wait for current task."""
        self._accepting = False
        task = self._current_task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(task, timeout=30.0)
            except TimeoutError:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

    # ------------------------------------------------------------------
    # Full scan implementation
    # ------------------------------------------------------------------

    async def _run_full_impl(self) -> ScrapeLog:
        """Core full-scan pipeline."""
        log_id: int | None = None
        total = 0
        matched = 0
        failed = 0
        detail_lines: list[str] = []

        try:
            # --- Create log entry (short transaction) ---
            with self._session_factory.begin() as sess:
                log = ScrapeLog(
                    started_at=datetime.now(UTC),
                    total=0, matched=0, failed=0,
                )
                sess.add(log)
                sess.flush()
                log_id = log.id

            # --- Load libraries (session closes immediately) ---
            with self._session_factory() as sess:
                libs = list(sess.execute(
                    select(Library).order_by(Library.id)
                ).scalars().all())

            # --- Scan directories ---
            fully_scanned: set[int] = set()
            found_paths: dict[int, set[str]] = {}

            for lib in libs:
                lib_path = Path(lib.path)
                try:
                    children = sorted(lib_path.iterdir())
                except OSError as exc:
                    detail_lines.append(f"库扫描失败: {lib.path}: {exc}")
                    continue

                found: set[str] = set()
                for child in children:
                    if not child.is_dir():
                        continue
                    if lib.media_type == "movie":
                        if contains_video(child):
                            found.add(normalize_path(str(child)))
                    else:  # tv
                        found.add(normalize_path(str(child)))

                # Upsert discovered items
                with self._session_factory.begin() as sess:
                    for fpath in found:
                        _upsert_item(sess, lib, fpath, self._config.overwrite_existing_nfo)

                fully_scanned.add(lib.id)
                found_paths[lib.id] = found

            # --- Mark missing ---
            with self._session_factory.begin() as sess:
                _mark_missing(sess, fully_scanned, found_paths)

            # --- Determine queue ---
            statuses = ["pending", "failed"]
            if self._config.overwrite_existing_nfo:
                statuses.append("matched")

            with self._session_factory() as sess:
                rows = sess.execute(
                    select(MediaItem.id).where(
                        MediaItem.library_id.in_(fully_scanned),
                        MediaItem.status.in_(statuses),
                    ).order_by(MediaItem.id)
                ).scalars().all()
                queue_ids = list(rows)

            total = len(queue_ids)

            # --- Phase 1: process NFO-skippable items locally ---
            api_queue_ids: list[int] = []
            for item_id in queue_ids:
                target = self._load_target(item_id)
                if target is None:
                    continue
                if (
                    not self._config.overwrite_existing_nfo
                    and target.status == "pending"
                    and nfo_exists(target.folder_path, target.media_type)
                ):
                    with self._session_factory.begin() as sess:
                        item = sess.get(MediaItem, target.id)
                        if item is not None:
                            item.status = "matched"
                            item.error_message = None
                    matched += 1
                else:
                    api_queue_ids.append(item_id)

            # --- Phase 2: scrape items requiring API ---
            for index, item_id in enumerate(api_queue_ids):
                target = self._load_target(item_id)
                if target is None:
                    continue
                try:
                    result = await self._scrape_one(target, force=False)
                    with self._session_factory.begin() as sess:
                        _persist_result(sess, target.id, result)
                    matched += 1
                except TmdbAuthError as exc:
                    # Batch-fail remaining API items
                    remaining = api_queue_ids[index:]
                    with self._session_factory.begin() as sess:
                        sess.execute(
                            update(MediaItem)
                            .where(MediaItem.id.in_(remaining))
                            .values(status="failed", error_message=str(exc))
                        )
                    failed += len(remaining)
                    for rid in remaining:
                        t = self._load_target(rid)
                        if t:
                            detail_lines.append(f"{t.folder_path}: {exc}")
                    break  # Stop sending requests
                except asyncio.CancelledError:
                    remaining = api_queue_ids[index:]
                    with self._session_factory.begin() as sess:
                        sess.execute(
                            update(MediaItem)
                            .where(MediaItem.id.in_(remaining))
                            .values(
                                status="failed",
                                error_message="任务因应用关闭而取消",
                            )
                        )
                    failed += len(remaining)
                    for rid in remaining:
                        t = self._load_target(rid)
                        if t:
                            detail_lines.append(f"{t.folder_path}: 任务取消")
                    raise
                except Exception as exc:  # noqa: BLE001 (item isolation — failure must not abort batch)
                    with self._session_factory.begin() as sess:
                        item = sess.get(MediaItem, item_id)
                        if item is not None:
                            item.status = "failed"
                            item.error_message = str(exc)
                    failed += 1
                    detail_lines.append(f"{target.folder_path}: {exc}")

        except asyncio.CancelledError:
            # Handle cancellation at the top level too
            raise
        except Exception:
            logger.exception("run_full encountered unexpected error")
            raise
        finally:
            # --- Finalise log entry ---
            if log_id is not None:
                with self._session_factory.begin() as sess:
                    existing_log = sess.get(ScrapeLog, log_id)
                    if existing_log is not None:
                        existing_log.finished_at = datetime.now(UTC)
                        existing_log.total = total
                        existing_log.matched = matched
                        existing_log.failed = failed
                        existing_log.detail = "\n".join(detail_lines) if detail_lines else None

        # Reload the log entry for return
        with self._session_factory() as sess:
            final_log = sess.get(ScrapeLog, log_id)
            if final_log is None:
                raise RuntimeError(f"ScrapeLog {log_id} not found after finalise")
            return final_log

    # ------------------------------------------------------------------
    # Single item rescrape
    # ------------------------------------------------------------------

    async def _rescrape_item_impl(self, item_id: int) -> MediaItem:
        target = self._load_target(item_id)
        if target is None:
            raise ItemNotFoundError(f"MediaItem {item_id} 不存在")

        log_id: int | None = None
        total = 1
        matched = 0
        failed = 0
        detail = ""

        with self._session_factory.begin() as sess:
            log = ScrapeLog(
                started_at=datetime.now(UTC),
                total=1, matched=0, failed=0,
            )
            sess.add(log)
            sess.flush()
            log_id = log.id

        try:
            result = await self._scrape_one(target, force=True)
            with self._session_factory.begin() as sess:
                _persist_result(sess, target.id, result)
            matched = 1
            detail = f"手动重刮: {target.folder_path}"
        except Exception as exc:  # noqa: BLE001 (rescrape must not raise — convert to failed status)
            with self._session_factory.begin() as sess:
                item = sess.get(MediaItem, item_id)
                if item is not None:
                    item.status = "failed"
                    item.error_message = str(exc)
            failed = 1
            detail = f"手动重刮: {target.folder_path}: {exc}"
        finally:
            if log_id is not None:
                with self._session_factory.begin() as sess:
                    rescrape_log = sess.get(ScrapeLog, log_id)
                    if rescrape_log is not None:
                        rescrape_log.finished_at = datetime.now(UTC)
                        rescrape_log.total = total
                        rescrape_log.matched = matched
                        rescrape_log.failed = failed
                        rescrape_log.detail = detail

        with self._session_factory() as sess:
            item = sess.get(MediaItem, item_id)
            if item is None:
                raise RuntimeError(f"MediaItem {item_id} disappeared after rescrape")
            return item

    # ------------------------------------------------------------------
    # Core scrape logic
    # ------------------------------------------------------------------

    async def _scrape_one(
        self, target: ScrapeTarget, *, force: bool,
    ) -> ScrapeResult:
        """Scrape a single item.  ``force`` bypasses NFO skip.

        Raises on failure; the caller persists the result or marks failed.
        """
        # Step 1: NFO skip check
        if (
            not force
            and not self._config.overwrite_existing_nfo
            and target.status == "pending"
            and nfo_exists(target.folder_path, target.media_type)
        ):
            return ExistingNfoMatched()

        # Step 2: must have a title
        if not target.parsed_title:
            raise ScrapeError("标题解析为空，无法搜索")

        # Step 3: TMDB search + detail
        meta = await self._tmdb.search_and_fetch(
            target.parsed_title, target.parsed_year, target.media_type,
        )
        if meta is None:
            raise ScrapeError("TMDB 无搜索结果")

        # Step 4: Douban supplement
        if self._config.use_douban and self._douban:
            try:
                supp = await self._douban.fetch_supplement(
                    target.parsed_title, meta.year,
                )
            except Exception:
                logger.warning(
                    "豆瓣模块异常(%s)", target.parsed_title, exc_info=True,
                )
                supp = None

            if supp is not None:
                if supp.overview is not None:
                    meta.overview = supp.overview
                if supp.rating is not None:
                    meta.rating = supp.rating

        # Step 5: Download images
        if meta.poster_url:
            await self._tmdb.download_image(
                meta.poster_url, target.folder_path / "poster.jpg",
            )
        if meta.backdrop_url:
            await self._tmdb.download_image(
                meta.backdrop_url, target.folder_path / "fanart.jpg",
            )

        # Step 6: Write NFO (last — completion marker before subtitles)
        if target.media_type == "movie":
            write_movie_nfo(target.folder_path, meta)
        else:
            write_tvshow_nfo(target.folder_path, meta)

        # Step 7: Subtitle download (best-effort, after NFO)
        if self._config.subtitle_enabled and self._subtitle is not None:
            try:
                await self._subtitle.download(
                    title=target.parsed_title or meta.title,
                    year=meta.year,
                    media_folder=target.folder_path,
                )
            except Exception:
                logger.warning(
                    "Subtitle download failed for %s", target.parsed_title, exc_info=True,
                )

        return meta

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_target(self, item_id: int) -> ScrapeTarget | None:
        """Load a MediaItem as an immutable ScrapeTarget, closing session immediately."""
        with self._session_factory() as sess:
            item = sess.get(MediaItem, item_id)
            if item is None:
                return None
            return ScrapeTarget(
                id=item.id,
                folder_path=Path(item.folder_path),
                media_type=item.media_type,
                parsed_title=item.parsed_title,
                parsed_year=item.parsed_year,
                status=item.status,
            )


# ---------------------------------------------------------------------------
# Internal: DB helpers used by ScanRunner
# ---------------------------------------------------------------------------


def _upsert_item(
    sess: Session,
    lib: Library,
    folder_path: str,
    overwrite: bool,
) -> MediaItem | None:
    """Insert or update a MediaItem for a discovered folder.

    Follows the state-machine table (§9.1).
    """
    parsed: ParsedName = parse_folder_name(Path(folder_path).name)

    # Check for existing record
    existing = sess.execute(
        select(MediaItem).where(MediaItem.folder_path == folder_path)
    ).scalar_one_or_none()

    if existing is not None:
        return _update_existing(sess, existing, lib, parsed, overwrite)

    # New record
    return _create_new(sess, lib, folder_path, parsed, overwrite)


def _create_new(
    sess: Session,
    lib: Library,
    folder_path: str,
    parsed: ParsedName,
    overwrite: bool,
) -> MediaItem:
    """Create a new MediaItem from a newly discovered folder."""
    has_nfo = nfo_exists(Path(folder_path), lib.media_type)

    if has_nfo and not overwrite:
        # NFO exists → skip to matched (even without title)
        status = "pending"  # enters queue, will be skipped as ExistingNfoMatched
    elif parsed.title is None:
        status = "manual_needed"
    else:
        status = "pending"

    item = MediaItem(
        library_id=lib.id,
        media_type=lib.media_type,
        folder_path=folder_path,
        parsed_title=parsed.title,
        parsed_year=parsed.year,
        status=status,
    )
    sess.add(item)
    return item


def _update_existing(
    sess: Session,
    item: MediaItem,
    lib: Library,
    parsed: ParsedName,
    overwrite: bool,
) -> MediaItem:
    """Update an existing MediaItem's parsed fields and status."""
    has_nfo = nfo_exists(Path(item.folder_path), lib.media_type)

    if item.status == "missing" or item.status == "manual_needed":
        # Re-discovered
        item.library_id = lib.id
        item.parsed_title = parsed.title
        item.parsed_year = parsed.year
        if has_nfo and not overwrite:
            item.status = "pending"
        elif parsed.title is None:
            item.status = "manual_needed"
        else:
            item.status = "pending"

    elif item.status == "failed":
        # Re-discovered
        item.library_id = lib.id
        if parsed.title:
            # Keep failed to force retry
            item.parsed_title = parsed.title
            item.parsed_year = parsed.year
        else:
            item.status = "manual_needed"

    elif item.status == "matched":
        item.library_id = lib.id
        if not has_nfo and not overwrite:
            # NFO was deleted → re-enter queue
            item.parsed_title = parsed.title
            item.parsed_year = parsed.year
            if parsed.title:
                item.status = "pending"
            else:
                item.status = "manual_needed"
        # Otherwise keep matched

    else:  # pending
        item.library_id = lib.id
        item.parsed_title = parsed.title
        item.parsed_year = parsed.year

    return item


def _mark_missing(
    sess: Session,
    fully_scanned: set[int],
    found_paths: dict[int, set[str]],
) -> None:
    """Mark items in fully-scanned libraries that weren't found on disk as missing."""
    if not fully_scanned:
        return

    all_items = sess.execute(
        select(MediaItem).where(
            MediaItem.library_id.in_(fully_scanned),
            MediaItem.status != "missing",
        )
    ).scalars().all()

    for item in all_items:
        lib_found = found_paths.get(item.library_id, set())
        if normalize_path(item.folder_path) not in lib_found:
            item.status = "missing"


def _persist_result(
    sess: Session,
    item_id: int,
    result: ScrapeResult,
) -> None:
    """Persist a scrape result back to the MediaItem row."""
    item = sess.get(MediaItem, item_id)
    if item is None:
        return

    if isinstance(result, ExistingNfoMatched):
        item.status = "matched"
        item.error_message = None
        return

    # ScrapedMeta
    item.source = result.source
    item.source_id = result.source_id
    item.matched_title = result.title
    item.matched_original_title = result.original_title
    item.matched_year = result.year
    item.overview = result.overview
    item.rating = result.rating
    item.poster_url = result.poster_url
    item.backdrop_url = result.backdrop_url
    item.genres = ",".join(result.genres)
    item.last_scraped_at = datetime.now(UTC)
    item.status = "matched"
    item.error_message = None
