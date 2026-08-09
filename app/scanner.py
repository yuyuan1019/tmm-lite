"""Scan and scrape orchestration (M7).

Core module that ties together directory scanning, database sync, NFO
detection, TMDB/Douban scraping, image download, and NFO writing.

Implements the definitive state machine from implementation spec §9.1.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import Callable, Coroutine, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, TypeVar

from sqlalchemy import select, update
from sqlalchemy.orm import Session, sessionmaker

from app import VIDEO_EXTENSIONS
from app.config import AppConfig
from app.connection import Connection, ConnectionConfig, LocalConnection, create_connection
from app.crypto import decrypt_dict
from app.database import AppMeta, Library, MediaItem, ScrapeLog
from app.exceptions import (
    ItemNotFoundError,
    ScanBusyError,
    ScrapeError,
    TmdbAuthError,
)
from app.nfo_writer import build_movie_nfo_bytes, build_tvshow_nfo_bytes, parse_nfo
from app.parsers.filename_parser import ParsedName, parse_folder_name
from app.scrapers.base import ScrapedMeta
from app.scrapers.douban import DoubanScraper
from app.scrapers.subtitle import SubtitleDownloader
from app.scrapers.tmdb import TmdbScraper

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScrapeTarget:
    """Immutable DTO passed from DB read to network scrape."""

    id: int
    library_id: int
    library_path: str
    connection_type: str
    connection_config_encrypted: str | None
    folder_path: str
    media_type: str
    parsed_title: str | None
    parsed_year: int | None
    status: str
    is_file: bool = False  # True for loose-file items (folder_path points at a video file)


class ExistingNfoMatched:
    """Marker: item was matched because a valid NFO already exists."""


ScrapeResult = ScrapedMeta | ExistingNfoMatched

# ---------------------------------------------------------------------------
# Discovery constants
# ---------------------------------------------------------------------------

# Sub-directories that are never media items themselves (skipped during recursion).
_NOISE_DIR_NAMES: frozenset[str] = frozenset({
    "extras", "trailer", "trailers", "sample", "samples", "screenshots",
    "featurettes", "behind the scenes", "deleted scenes", "interviews",
    "making of", "outtakes", "bonus", "extra", ".actors", ".backdrops",
    "logo", "scrapbook",
})

# A directory is a TV show when it directly holds a Season-like sub-folder
# (English "Season 01", compact "S01", or Chinese "第1季").  Extra text after
# the season marker is allowed (e.g. "第5季（2019）", "Season 01 (2020)").
# No leading anchor — .match() gives start-anchoring; .search() finds mid-name.
_RE_SEASON_DIR = re.compile(r"(?i)(?:season[ ._]?\d{1,2}|s\d{1,2}|第\d{1,2}季)")

# Collection / box-set container folders — these hold multiple movies and
# should never be scraped as a single title.
_RE_COLLECTION_DIR = re.compile(
    r"(?i)(?:collection|box[ ._-]?set|trilogy|tetralogy|quadrilogy|anthology|"
    r"全集|合集|集锦|大合集|电影合集|系列合集|合集版|套装|合辑|"
    r"\d+[ ._-]*film[ ._-]*collection|"
    r"\d+[ ._-]*movie[ ._-]*collection|"
    r"\d+部)"
)

# Disc-structure sub-folders that mark a movie folder (folder_path = parent).
_DISC_STRUCTURE_DIRS: frozenset[str] = frozenset({"bdmv", "video_ts"})

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def contains_video(folder: Path) -> bool:
    """Check if *folder* contains at least one video file (max depth 2)."""
    return find_video_file(folder) is not None


def find_video_file(folder: Path) -> Path | None:
    """Return the first video file found under *folder* (depth ≤2), or None."""
    try:
        for child in folder.iterdir():
            if child.is_file() and child.suffix.lower() in VIDEO_EXTENSIONS:
                return child
            if child.is_dir():
                for sub in child.iterdir():
                    if sub.is_file() and sub.suffix.lower() in VIDEO_EXTENSIONS:
                        return sub
    except OSError:
        pass
    return None


def normalize_path(path: str) -> str:
    """Normalise a path to an absolute POSIX string.

    Uses ``os.path.normpath`` — does **not** resolve symlinks.

    Deliberately avoids ``os.path.abspath``: on Windows it rewrites a
    POSIX-absolute path (e.g. a *remote* SSH/WebDAV ``/Download/movies``)
    onto the current drive (``D:\\Download\\movies``), corrupting remote
    library paths.  All callers pass absolute paths (library paths are
    validated to start with ``/``; folder paths are built from
    ``PurePosixPath(lib.path) / rel``), so plain ``normpath`` is enough and
    keeps remote paths intact.
    """
    norm = os.path.normpath(path)
    return norm.replace("\\", "/")


def _safe_int(value: object, default: int = 0) -> int:
    """Convert a dynamic configuration value to ``int`` without escaping errors."""
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _relative_folder(folder_path: str, library_path: str) -> str:
    """Return *folder_path* relative to *library_path* as a POSIX string."""
    fp = PurePosixPath(normalize_path(folder_path))
    lp = PurePosixPath(normalize_path(library_path))
    return str(fp.relative_to(lp))


def _nfo_filename(media_type: str) -> str:
    """Return the NFO filename for a media type."""
    return "movie.nfo" if media_type == "movie" else "tvshow.nfo"


def _is_file_item(rel: str) -> bool:
    """True when *rel* points at a video file rather than a folder.

    Loose-file items store the full video-file path in ``folder_path``, so
    their relative path still ends in a video extension.
    """
    return Path(rel).suffix.lower() in VIDEO_EXTENSIONS


def _nfo_rel(rel: str, media_type: str) -> str:
    """Return the NFO path relative to the item's media location.

    Folder items keep the Kodi folder convention (``movie.nfo`` / ``tvshow.nfo``
    inside the folder); loose-file items use the per-file convention
    (``<video-stem>.nfo`` next to the video file).
    """
    if _is_file_item(rel):
        return str(PurePosixPath(rel).with_suffix(".nfo"))
    return str(PurePosixPath(rel) / _nfo_filename(media_type))


def _image_rel(rel: str, kind: str) -> str:
    """Return the poster/fanart path relative to the item's media location.

    ``kind`` is ``"poster"`` or ``"fanart"``.  Folder items keep ``poster.jpg``
    / ``fanart.jpg``; loose-file items use ``<stem>-poster.jpg`` etc. next to
    the video file (Kodi per-file convention).
    """
    if _is_file_item(rel):
        suffix = "-poster.jpg" if kind == "poster" else "-fanart.jpg"
        return str(PurePosixPath(rel).with_name(PurePosixPath(rel).stem + suffix))
    name = "poster.jpg" if kind == "poster" else "fanart.jpg"
    return str(PurePosixPath(rel) / name)


# ---------------------------------------------------------------------------
# Subtitle-status detection (for the items list display)
# ---------------------------------------------------------------------------

_SUBTITLE_EXTS = frozenset({".srt", ".ass", ".ssa", ".sub", ".vtt", ".idx", ".sup"})
_RE_CJK = re.compile(r"[一-鿿]")


def detect_subtitle_summary(names: Iterable[str]) -> str | None:
    """Classify subtitle files under one item for display.

    Returns ``"简体中文"`` / ``"繁体中文"`` / ``"中文"`` / ``"有字幕(非中文)"``,
    or ``None`` when there is no subtitle file.
    """
    subs = [n for n in names if Path(n).suffix.lower() in _SUBTITLE_EXTS]
    if not subs:
        return None

    _SIMPLIFIED = ("zh-hans", "zh_hans", "zh-cn", "zh_cn", "chs", "简体", "简中")
    _TRADITIONAL = ("zh-hant", "zh_hant", "zh-tw", "zh_tw", "cht", "繁体", "繁中")

    for n in subs:
        low = n.lower()
        if _RE_CJK.search(n):
            if any(m in low for m in _SIMPLIFIED):
                return "简体中文"
            if any(m in low for m in _TRADITIONAL):
                return "繁体中文"
            return "中文"
        if any(m in low for m in _SIMPLIFIED):
            return "简体中文"
        if any(m in low for m in _TRADITIONAL):
            return "繁体中文"
        if re.search(r"(?:^|[._\-])zh(?:[._\-]|$)", low) or "zho" in low or "chi" in low:
            return "中文"
    return "有字幕(非中文)"


async def _detect_subtitle_summary_async(conn: Connection, rel: str) -> str | None:
    """Detect the subtitle summary for the item at relative path *rel*."""
    if _is_file_item(rel):
        rel = str(PurePosixPath(rel).parent)
    try:
        names = await conn.list_dir(rel)
    except OSError:
        return None
    return detect_subtitle_summary(names)


def _library_connection(lib: Library, enc_key: bytes | None) -> Connection:
    """Create a :class:`Connection` for *lib*.

    Local libraries always use :class:`LocalConnection`.
    Remote libraries decrypt their stored config and create the appropriate
    connection.  Returns ``LocalConnection`` as a safe fallback when no key
    is available.
    """
    if lib.connection_type == "local" or not lib.connection_type:
        return LocalConnection(lib.path)

    cfg_enc = lib.connection_config_encrypted
    if not cfg_enc or enc_key is None:
        logger.warning(
            "Library %s is remote but has no credentials/key, falling back to local",
            lib.path,
        )
        return LocalConnection(lib.path)

    try:
        cfg = decrypt_dict(cfg_enc, enc_key)
    except Exception as exc:  # noqa: BLE001 (decrypt may raise various errors)
        logger.warning("Failed to decrypt connection config for %s: %s", lib.path, exc)
        return LocalConnection(lib.path)

    conn_cfg = ConnectionConfig(
        type=lib.connection_type,
        host=str(cfg.get("host", "")),
        port=_safe_int(cfg.get("port")),
        username=str(cfg.get("username", "")),
        password=str(cfg.get("password", "")),
    )
    return create_connection(conn_cfg, lib.path)


async def _nfo_exists_async(conn: Connection, rel_folder: str, media_type: str) -> bool:
    """Check whether the NFO file exists for *rel_folder* via *conn*."""
    nfo_rel = _nfo_rel(rel_folder, media_type)
    try:
        return await conn.exists(nfo_rel)
    except OSError:
        return False


async def _read_nfo_meta_async(
    conn: Connection, rel_folder: str, media_type: str,
) -> dict[str, object] | None:
    """Read & parse the existing NFO for *rel_folder* via *conn*.

    Returns the parsed metadata dict, or ``None`` if the NFO is missing or
    cannot be parsed. Used to load a folder's bundled NFO without scraping.
    """
    nfo_rel = _nfo_rel(rel_folder, media_type)
    try:
        data = await conn.read_bytes(nfo_rel)
    except Exception:  # noqa: BLE001 (best-effort; treat as "no usable meta")
        return None
    return parse_nfo(data)


async def _find_video_file_async(conn: Connection, rel_folder: str) -> str | None:
    """Return the relative path of the first video file under *rel_folder*."""
    try:
        entries = await conn.list_dir(rel_folder)
    except OSError:
        return None
    for name in entries:
        child_rel = str(PurePosixPath(rel_folder) / name)
        try:
            if await conn.is_file(child_rel):
                if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                    return child_rel
            elif await conn.is_dir(child_rel):
                subs = await conn.list_dir(child_rel)
                for sub in subs:
                    sub_rel = str(PurePosixPath(child_rel) / sub)
                    if await conn.is_file(sub_rel) and Path(sub).suffix.lower() in VIDEO_EXTENSIONS:
                        return sub_rel
        except OSError:
            continue
    return None


def _library_connection_from_target(target: ScrapeTarget, enc_key: bytes | None) -> Connection:
    """Create a :class:`Connection` from a :class:`ScrapeTarget`."""
    lib = Library(
        path=target.library_path,
        media_type=target.media_type,
        connection_type=target.connection_type,
        connection_config_encrypted=target.connection_config_encrypted,
    )
    return _library_connection(lib, enc_key)


async def _discover_folders(conn: Connection, lib: Library) -> set[str]:
    """Return the set of absolute POSIX item paths discovered under *lib*.

    Item paths are folders for folder-based items and full video-file paths
    for loose files directly in the library root.  Discovery recurses, so
    genre/director-grouped and deeply nested layouts are covered.  Raises
    ``OSError`` when the library root cannot be read.
    """
    found: set[str] = set()
    await _discover_walk(conn, lib, "", found, is_root=True)
    return found


async def _discover_walk(
    conn: Connection, lib: Library, rel: str, found: set[str], *, is_root: bool,
) -> None:
    """Recursively collect movie/TV item paths under directory *rel*.

    *rel* is ``""`` for the library root.  ``found`` receives normalized
    absolute paths (folders, or video-file paths for root loose files).
    """
    entries = await conn.list_dir(rel)
    videos: list[str] = []
    subdirs: list[str] = []
    has_disc_subdir = False
    for name in entries:
        child_rel = name if not rel else str(PurePosixPath(rel) / name)
        try:
            if await conn.is_file(child_rel):
                if Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                    videos.append(name)
            elif await conn.is_dir(child_rel):
                lower = name.lower()
                if lower in _DISC_STRUCTURE_DIRS:
                    has_disc_subdir = True
                elif lower not in _NOISE_DIR_NAMES:
                    subdirs.append(name)
        except OSError:
            continue

    if lib.media_type == "movie":
        if is_root:
            # Loose videos in the library root are individual file items.
            for v in videos:
                found.add(normalize_path(str(PurePosixPath(lib.path) / v)))
        elif _RE_COLLECTION_DIR.search(Path(rel).name):
            # Collection / box-set container folder — recurse into its
            # children but do NOT treat the folder itself as a movie.
            for d in subdirs:
                await _discover_walk(conn, lib, _join_rel(rel, d), found, is_root=False)
            return
        elif videos or has_disc_subdir:
            # A folder that directly holds a video — or a disc structure
            # (Movie/BDMV, Movie/VIDEO_TS) — is one movie.  Stop descending so
            # Extras/Featurette sub-folders are not counted as movies.
            found.add(normalize_path(str(PurePosixPath(lib.path) / rel)))
            return
        elif await _looks_like_deep_movie(conn, rel, subdirs):
            # e.g. "Movie (2020)/Video/movie.mkv": this folder has no direct
            # video but exactly one sub-folder that does, and its own name
            # parses to a titled+year movie → this folder is the movie.
            found.add(normalize_path(str(PurePosixPath(lib.path) / rel)))
            return
        for d in subdirs:
            await _discover_walk(conn, lib, _join_rel(rel, d), found, is_root=False)
    else:  # tv
        if not is_root and _RE_COLLECTION_DIR.search(Path(rel).name):
            # Collection / box-set container — recurse, don't add as a show.
            for d in subdirs:
                await _discover_walk(conn, lib, _join_rel(rel, d), found, is_root=False)
            return
        if not is_root and (
            videos
            or any(_RE_SEASON_DIR.match(s) for s in subdirs)
            or _looks_like_episode_container(rel, subdirs)
        ):
            # A folder holding episodes directly, Season sub-folders, or
            # per-episode sub-folders (第3集 / S01E02) is one show.  Stop
            # descending so Season/episode folders are not counted as shows.
            found.add(normalize_path(str(PurePosixPath(lib.path) / rel)))
            return
        for d in subdirs:
            await _discover_walk(conn, lib, _join_rel(rel, d), found, is_root=False)


def _join_rel(rel: str, name: str) -> str:
    """Join a directory-relative path onto *rel* (``""`` for the root)."""
    return name if not rel else str(PurePosixPath(rel) / name)


async def _looks_like_deep_movie(conn: Connection, rel: str, subdirs: list[str]) -> bool:
    """True when *rel* is a deeply-nested movie folder.

    Matches layouts like ``Movie (2020)/Video/movie.mkv`` where the movie
    folder itself holds no video but exactly one of its sub-folders does, and
    its own name parses to a titled + year movie.
    """
    if len(subdirs) != 1:
        return False
    parsed = parse_folder_name(Path(rel).name)
    if not parsed.title or parsed.year is None:
        return False
    try:
        return await conn.contains_video(_join_rel(rel, subdirs[0]))
    except OSError:
        return False


def _is_episode_only_name(name: str) -> bool:
    """True when *name* is an episode or season label with no show title.

    Examples: ``第3集``, ``S01E02``, ``第5季（2019）``, ``Season 01``.
    """
    parsed = parse_folder_name(name)
    if parsed.title is not None:
        return False
    return parsed.episode is not None or parsed.season is not None


def _looks_like_episode_container(rel: str, subdirs: list[str]) -> bool:
    """True when *rel* is a show whose episodes/seasons live one-per-folder.

    Matches e.g. ``黑镜 Black Mirror[全7季]/第3集（2016）/`` — the show folder's
    own name parses to a title and its immediate sub-folders are episode/season
    labels.
    """
    if not subdirs:
        return False
    parsed = parse_folder_name(Path(rel).name)
    if not parsed.title:
        return False
    for s in subdirs:
        # Check 1: episode/season-only name ("第3集", "S01E02", "第5季（2019）")
        if _is_episode_only_name(s):
            return True
        # Check 2: name contains a season pattern anywhere
        # (e.g. "无耻家庭.第04季.Shameless.S04.2014..." — has S04 mid-name)
        if _RE_SEASON_DIR.search(s):
            return True
        # Check 3: parsed season/episode number is present
        # (even if the folder name also has a title)
        sp = parse_folder_name(s)
        if sp.season is not None or sp.episode is not None:
            return True
    return False


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
        enc_key: bytes | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._config = config
        self._tmdb = tmdb
        self._douban = douban
        self._enc_key = enc_key
        self._subtitle: SubtitleDownloader | None = None
        self._running = False
        self._accepting = True
        self._current_task: asyncio.Task[Any] | None = None
        self._stop_requested = False
        self._progress: list[str] = []

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
        self._stop_requested = False
        self._progress = []

    def _release(self) -> None:
        """Release the internal mutex."""
        if self._current_task is asyncio.current_task():
            self._current_task = None
        self._running = False

    @property
    def is_running(self) -> bool:
        return self._running

    def _log_progress(self, line: str) -> None:
        """Append a timestamped line to the in-memory live-progress buffer."""
        ts = datetime.now(UTC).astimezone().strftime("%H:%M:%S")
        self._progress.append(f"[{ts}] {line}")

    def progress_lines(self, limit: int = 200) -> list[str]:
        """Return the most recent live-progress lines (for the /scan-live page)."""
        return self._progress[-limit:]

    # ------------------------------------------------------------------
    # Configuration hot-reload
    # ------------------------------------------------------------------

    def set_subtitle_downloader(self, downloader: SubtitleDownloader | None) -> None:
        """Configure the subtitle downloader (created after runner init)."""
        if downloader is not None:
            downloader._on_progress = self._log_progress
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
        return self._start_background(self._run_full_impl)

    def start_rescrape_failed_background(self) -> asyncio.Task[ScrapeLog]:
        """Start a background run that re-scrapes all failed items."""
        return self._start_background(self._rescrape_failed_impl)

    def _start_background(
        self, factory: Callable[[], Coroutine[Any, Any, T]],
    ) -> asyncio.Task[T]:
        """Claim the mutex and run a freshly-created coroutine in the background.

        Claiming happens before invoking *factory*, so a rejected start never
        creates a coroutine that could be left un-awaited.  The done callback
        clears ownership only while its task is still the active task.
        """
        self._claim()
        coro: Coroutine[Any, Any, T] | None = None
        try:
            coro = factory()
            task = asyncio.create_task(coro)
            self._current_task = task

            def _done(t: asyncio.Task[T]) -> None:
                if self._current_task is t:
                    self._current_task = None
                    self._running = False
                if not t.cancelled():
                    exc = t.exception()
                    if exc is not None:
                        logger.error("Background scan failed: %s", exc)

            task.add_done_callback(_done)
            return task
        except BaseException:
            if coro is not None:
                coro.close()
            self._release()
            raise

    async def rescrape_failed(self) -> ScrapeLog:
        """Re-scrape all items in ``failed`` status (awaited form)."""
        self._claim()
        try:
            return await self._rescrape_failed_impl()
        finally:
            self._release()

    def stop(self) -> bool:
        """Gracefully stop a running scan (used by the web "停止" button).

        Cancels the current scan task; remaining queued items are marked
        ``failed`` with a manual-stop message.  Returns ``True`` when a scan
        was running and a stop was requested, ``False`` when idle.

        Concurrency: this is a plain sync call — it only sets a flag and
        schedules the cancellation, so it cannot deadlock with the scan task.
        The mutex is released by the task's done-callback once the task
        actually unwinds, at which point ``is_running`` goes ``False`` and a
        new scan may start.
        """
        if not self._running:
            return False
        task = self._current_task
        if task is None or task.done():
            return False
        self._stop_requested = True
        task.cancel()
        return True

    async def rescrape_item(
        self, item_id: int, *, query: str | None = None, tmdb_id: int | None = None,
    ) -> MediaItem:
        """Force re-scrape a single item (ignores existing NFO).

        ``query`` overrides the search title; ``tmdb_id`` forces a specific TMDB
        match (both come from the manual-match dialog).
        """
        self._claim()
        try:
            return await self._rescrape_item_impl(item_id, query=query, tmdb_id=tmdb_id)
        finally:
            self._release()

    def start_rescrape_item_background(
        self, item_id: int, *, query: str | None = None, tmdb_id: int | None = None,
    ) -> asyncio.Task[MediaItem]:
        """Like :meth:`rescrape_item` but returns immediately in a claimed task."""
        return self._start_background(
            lambda: self._rescrape_item_impl(item_id, query=query, tmdb_id=tmdb_id)
        )

    async def download_subtitle(self, item_id: int) -> Path | None:
        """Manually download subtitles for a single item.

        Returns the saved subtitle path, or ``None`` when no subtitle matched.
        Raises :class:`ItemNotFoundError` when the item is missing and
        :class:`ScanBusyError` when a scan is already running.
        """
        self._claim()
        try:
            return await self._download_subtitle_impl(item_id)
        finally:
            self._release()

    def start_refresh_subtitles_background(self) -> asyncio.Task[ScrapeLog]:
        """Start a background task that downloads subtitles for all matched
        items whose title is not primarily Chinese (non-CJK matched title)."""
        return self._start_background(self._refresh_subtitles_impl)

    def start_rescan_library_background(self, lib_id: int) -> asyncio.Task[ScrapeLog]:
        """Re-scan a single library in the background (re-discover + upsert)."""
        return self._start_background(lambda: self._rescan_library_impl(lib_id))

    async def rescan_library(self, lib_id: int) -> ScrapeLog:
        """Re-scan a single library synchronously."""
        self._claim()
        try:
            return await self._rescan_library_impl(lib_id)
        finally:
            self._release()

    async def _rescan_library_impl(self, lib_id: int) -> ScrapeLog:
        """Re-discover folders for one library and upsert items."""
        log_id: int | None = None
        total = matched = failed = 0
        detail_lines: list[str] = []

        with self._session_factory.begin() as sess:
            log = ScrapeLog(
                started_at=datetime.now(UTC),
                total=0, matched=0, failed=0,
            )
            sess.add(log)
            sess.flush()
            log_id = log.id

        try:
            with self._session_factory() as sess:
                lib = sess.get(Library, lib_id)
                if lib is None:
                    raise ItemNotFoundError(f"Library {lib_id} 不存在")
                lib_path = lib.path
                lib_name = lib.name

            conn = _library_connection(lib, self._enc_key)
            self._log_progress(f"重新扫描库: {lib_name} ({lib_path})")
            try:
                found = await _discover_folders(conn, lib)
                with self._session_factory() as sess:
                    ignored = _ignored_paths(sess)
                if ignored:
                    found = {p for p in found if p not in ignored}

                nfo_map: dict[str, bool] = {}
                nfo_meta_map: dict[str, dict[str, object] | None] = {}
                for fpath in found:
                    rel = _relative_folder(fpath, lib_path)
                    has = await _nfo_exists_async(conn, rel, lib.media_type)
                    nfo_map[fpath] = has
                    if has:
                        nfo_meta_map[fpath] = await _read_nfo_meta_async(conn, rel, lib.media_type)

                with self._session_factory.begin() as sess:
                    for fpath in found:
                        _upsert_item(
                            sess, lib, fpath, self._config.overwrite_existing_nfo,
                            has_nfo=nfo_map.get(fpath, False),
                            nfo_meta=nfo_meta_map.get(fpath),
                        )

                # Mark missing for this library
                with self._session_factory.begin() as sess:
                    _mark_missing(sess, {lib_id}, {lib_id: found})

                total = len(found)
                detail_lines.append(f"库 {lib_name}: 发现 {total} 个条目")
                self._log_progress(f"库 {lib_name}: 发现 {total} 个条目，重新扫描完成")
            except Exception:
                logger.exception("库重新扫描失败: %s", lib_path)
                detail_lines.append(f"库重新扫描失败: {lib_path}")
                failed = 1
            finally:
                await conn.aclose()

            # Queue found items for scraping (same logic as full scan)
            statuses = ["pending", "failed"]
            if self._config.overwrite_existing_nfo:
                statuses.append("matched")

            with self._session_factory() as sess:
                rows = sess.execute(
                    select(MediaItem.id).where(
                        MediaItem.library_id == lib_id,
                        MediaItem.status.in_(statuses),
                    ).order_by(MediaItem.id)
                ).scalars().all()
                queue_ids = list(rows)

            # Phase 1: NFO-skippable
            api_queue_ids: list[int] = []
            for item_id in queue_ids:
                target = self._load_target(item_id)
                if target is None:
                    continue
                skip = False
                if not self._config.overwrite_existing_nfo and target.status == "pending":
                    conn2 = _library_connection_from_target(target, self._enc_key)
                    try:
                        rel2 = _relative_folder(target.folder_path, target.library_path)
                        skip = await _nfo_exists_async(conn2, rel2, target.media_type)
                    finally:
                        await conn2.aclose()
                if skip:
                    with self._session_factory.begin() as sess:
                        item = sess.get(MediaItem, item_id)
                        if item is not None:
                            item.status = "matched"
                            item.error_message = None
                    matched += 1
                else:
                    api_queue_ids.append(item_id)

            # Phase 2: scrape
            for index, item_id in enumerate(api_queue_ids):
                target = self._load_target(item_id)
                if target is None:
                    continue
                conn3 = _library_connection_from_target(target, self._enc_key)
                self._log_progress(f"正在刮削: {target.folder_path}")
                try:
                    result = await self._scrape_one(target, force=False, conn=conn3)
                    with self._session_factory.begin() as sess:
                        _persist_result(sess, target.id, result)
                    matched += 1
                    self._log_progress(f"成功: {target.folder_path}")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    with self._session_factory.begin() as sess:
                        item = sess.get(MediaItem, item_id)
                        if item is not None:
                            item.status = "failed"
                            item.error_message = str(exc)
                    failed += 1
                    detail_lines.append(f"{target.folder_path}: {exc}")
                    self._log_progress(f"失败: {target.folder_path}: {exc}")
                finally:
                    await conn3.aclose()

        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("rescan_library failed")
            raise
        finally:
            if log_id is not None:
                with self._session_factory.begin() as sess:
                    existing_log = sess.get(ScrapeLog, log_id)
                    if existing_log is not None:
                        existing_log.finished_at = datetime.now(UTC)
                        existing_log.total = total
                        existing_log.matched = matched
                        existing_log.failed = failed
                        existing_log.detail = "\n".join(detail_lines) if detail_lines else None
            self._log_progress(f"库重新扫描完成: 发现 {total} / 成功 {matched} / 失败 {failed}")

        with self._session_factory() as sess:
            final_log = sess.get(ScrapeLog, log_id)
            if final_log is None:
                raise RuntimeError(f"ScrapeLog {log_id} not found after rescan")
            return final_log

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
                conn = _library_connection(lib, self._enc_key)
                self._log_progress(f"扫描库: {lib.name} ({lib.path})")
                try:
                    found = await _discover_folders(conn, lib)
                    # Skip paths the user deleted from the list (record only —
                    # files on disk are untouched and re-added on un-ignore).
                    with self._session_factory() as sess:
                        ignored = _ignored_paths(sess)
                    if ignored:
                        found = {p for p in found if p not in ignored}
                    # Pre-compute NFO existence (+ parsed metadata) for discovered folders
                    nfo_map: dict[str, bool] = {}
                    nfo_meta_map: dict[str, dict[str, object] | None] = {}
                    for fpath in found:
                        rel = _relative_folder(fpath, lib.path)
                        has = await _nfo_exists_async(conn, rel, lib.media_type)
                        nfo_map[fpath] = has
                        if has:
                            nfo_meta_map[fpath] = await _read_nfo_meta_async(conn, rel, lib.media_type)

                    # Upsert discovered items
                    with self._session_factory.begin() as sess:
                        for fpath in found:
                            _upsert_item(
                                sess, lib, fpath, self._config.overwrite_existing_nfo,
                                has_nfo=nfo_map.get(fpath, False),
                                nfo_meta=nfo_meta_map.get(fpath),
                            )

                    fully_scanned.add(lib.id)
                    found_paths[lib.id] = found
                except Exception:
                    logger.exception("库扫描失败: %s", lib.path)
                    detail_lines.append(f"库扫描失败: {lib.path}")
                finally:
                    await conn.aclose()

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
                skip = False
                nfo_meta: dict[str, object] | None = None
                if (
                    not self._config.overwrite_existing_nfo
                    and target.status == "pending"
                ):
                    conn = _library_connection_from_target(target, self._enc_key)
                    try:
                        rel = _relative_folder(target.folder_path, target.library_path)
                        skip = await _nfo_exists_async(conn, rel, target.media_type)
                        if skip:
                            nfo_meta = await _read_nfo_meta_async(conn, rel, target.media_type)
                    finally:
                        await conn.aclose()
                if skip:
                    with self._session_factory.begin() as sess:
                        item = sess.get(MediaItem, item_id)
                        if item is not None:
                            if nfo_meta:
                                _apply_nfo_meta(item, nfo_meta, datetime.now(UTC))
                            else:
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
                conn = _library_connection_from_target(target, self._enc_key)
                self._log_progress(f"正在刮削: {target.folder_path}")
                try:
                    result = await self._scrape_one(target, force=False, conn=conn)
                    with self._session_factory.begin() as sess:
                        _persist_result(sess, target.id, result)
                    matched += 1
                    self._log_progress(f"成功: {target.folder_path}")
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
                    self._log_progress(f"TMDB 认证失败，批量中止剩余 {len(remaining)} 条: {exc}")
                    break  # Stop sending requests
                except asyncio.CancelledError:
                    stop_msg = "任务已手动停止" if self._stop_requested else "任务因应用关闭而取消"
                    remaining = api_queue_ids[index:]
                    with self._session_factory.begin() as sess:
                        sess.execute(
                            update(MediaItem)
                            .where(MediaItem.id.in_(remaining))
                            .values(
                                status="failed",
                                error_message=stop_msg,
                            )
                        )
                    failed += len(remaining)
                    for rid in remaining:
                        t = self._load_target(rid)
                        if t:
                            detail_lines.append(f"{t.folder_path}: {stop_msg}")
                    self._log_progress(stop_msg)
                    raise
                except Exception as exc:  # noqa: BLE001 (item isolation — failure must not abort batch)
                    with self._session_factory.begin() as sess:
                        item = sess.get(MediaItem, item_id)
                        if item is not None:
                            item.status = "failed"
                            item.error_message = str(exc)
                    failed += 1
                    detail_lines.append(f"{target.folder_path}: {exc}")
                    self._log_progress(f"失败: {target.folder_path}: {exc}")

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
            self._log_progress(f"扫描完成: 总计 {total} / 成功 {matched} / 失败 {failed}")

        # Reload the log entry for return
        with self._session_factory() as sess:
            final_log = sess.get(ScrapeLog, log_id)
            if final_log is None:
                raise RuntimeError(f"ScrapeLog {log_id} not found after finalise")
            return final_log

    # ------------------------------------------------------------------
    # Single item rescrape
    # ------------------------------------------------------------------

    async def _rescrape_item_impl(
        self, item_id: int, *, query: str | None = None, tmdb_id: int | None = None,
    ) -> MediaItem:
        target = self._load_target(item_id)
        if target is None:
            raise ItemNotFoundError(f"MediaItem {item_id} 不存在")

        # Re-parse the folder/file name with the current parser: the stored
        # parsed_title can be stale (parsed by an older parser version), which
        # would make the search fail even after a parser fix.  A manual
        # ``query``/``tmdb_id`` override takes precedence and is passed straight
        # to _scrape_one.
        fresh = parse_folder_name(Path(target.folder_path).name)
        if fresh.title:
            target = replace(
                target, parsed_title=fresh.title, parsed_year=fresh.year,
            )

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

        conn = _library_connection_from_target(target, self._enc_key)
        self._log_progress(f"正在刮削: {target.folder_path}")
        try:
            result = await self._scrape_one(
                target, force=True, conn=conn, tmdb_id=tmdb_id, query=query,
            )
            with self._session_factory.begin() as sess:
                _persist_result(sess, target.id, result)
            matched = 1
            self._log_progress(f"成功: {target.folder_path}")
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
            await conn.aclose()
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
    # Re-scrape all failed items
    # ------------------------------------------------------------------

    async def _rescrape_failed_impl(self) -> ScrapeLog:
        """Re-scrape every item in ``failed`` status (one-click action).

        Mirrors the per-item loop of a full scan but is scoped to ``failed``
        items only.  TMDB request frequency is throttled by the scraper's rate
        limiter (``tmdb_delay_seconds``).
        """
        with self._session_factory() as sess:
            ids = list(sess.execute(
                select(MediaItem.id).where(MediaItem.status == "failed")
            ).scalars().all())

        log_id: int | None = None
        total = len(ids)
        matched = 0
        failed = 0
        detail_lines: list[str] = []

        with self._session_factory.begin() as sess:
            log = ScrapeLog(
                started_at=datetime.now(UTC),
                total=total, matched=0, failed=0,
            )
            sess.add(log)
            sess.flush()
            log_id = log.id

        for index, item_id in enumerate(ids):
            target = self._load_target(item_id)
            if target is None:
                continue
            conn = _library_connection_from_target(target, self._enc_key)
            self._log_progress(f"正在刮削: {target.folder_path}")
            try:
                result = await self._scrape_one(target, force=True, conn=conn)
                with self._session_factory.begin() as sess:
                    _persist_result(sess, target.id, result)
                matched += 1
                self._log_progress(f"成功: {target.folder_path}")
            except TmdbAuthError as exc:
                remaining = ids[index:]
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
                self._log_progress(f"TMDB 认证失败，批量中止剩余 {len(remaining)} 条: {exc}")
                break  # Stop sending requests
            except asyncio.CancelledError:
                stop_msg = "任务已手动停止" if self._stop_requested else "任务因应用关闭而取消"
                remaining = ids[index:]
                with self._session_factory.begin() as sess:
                    sess.execute(
                        update(MediaItem)
                        .where(MediaItem.id.in_(remaining))
                        .values(status="failed", error_message=stop_msg)
                    )
                failed += len(remaining)
                for rid in remaining:
                    t = self._load_target(rid)
                    if t:
                        detail_lines.append(f"{t.folder_path}: {stop_msg}")
                self._log_progress(stop_msg)
                raise
            except Exception as exc:  # noqa: BLE001 (item isolation — failure must not abort batch)
                with self._session_factory.begin() as sess:
                    item = sess.get(MediaItem, item_id)
                    if item is not None:
                        item.status = "failed"
                        item.error_message = str(exc)
                failed += 1
                detail_lines.append(f"{target.folder_path}: {exc}")
                self._log_progress(f"失败: {target.folder_path}: {exc}")
            finally:
                await conn.aclose()

        if log_id is not None:
            with self._session_factory.begin() as sess:
                existing_log = sess.get(ScrapeLog, log_id)
                if existing_log is not None:
                    existing_log.finished_at = datetime.now(UTC)
                    existing_log.total = total
                    existing_log.matched = matched
                    existing_log.failed = failed
                    existing_log.detail = "\n".join(detail_lines) if detail_lines else None
            self._log_progress(f"重刮失败项完成: 总计 {total} / 成功 {matched} / 失败 {failed}")

        with self._session_factory() as sess:
            final_log = sess.get(ScrapeLog, log_id)
            if final_log is None:
                raise RuntimeError(f"ScrapeLog {log_id} not found after finalise")
            return final_log

    # ------------------------------------------------------------------
    # Single item subtitle download
    # ------------------------------------------------------------------

    async def _download_subtitle_impl(self, item_id: int) -> Path | None:
        """Manual subtitle download for a single item (mutex already held).

        Records a :class:`ScrapeLog` entry so manual subtitle attempts (found or
        not) are visible on the /logs page.
        """
        if not self._config.subtitle_enabled or self._subtitle is None:
            raise ScrapeError("字幕功能未启用，请在设置中开启")

        target = self._load_target(item_id)
        if target is None:
            raise ItemNotFoundError(f"MediaItem {item_id} 不存在")

        # Prefer the matched original (usually English) title + imdb_id for a
        # better subtitle search; fall back to the parsed/localized title.
        title: str = target.parsed_title or ""
        year: int | None = target.parsed_year
        imdb_id: str | None = None
        with self._session_factory() as sess:
            item = sess.get(MediaItem, item_id)
            if item is not None:
                if item.matched_original_title:
                    title = item.matched_original_title
                elif item.matched_title:
                    title = item.matched_title
                year = item.matched_year or item.parsed_year
                imdb_id = item.imdb_id

        if not title:
            raise ScrapeError("标题解析为空，无法搜索字幕")

        log_id: int | None = None
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

        conn = _library_connection_from_target(target, self._enc_key)
        self._log_progress(f"正在下载字幕: {target.folder_path}")
        try:
            result = await self._download_subtitle_for_target(
                target, conn, title=title, year=year, imdb_id=imdb_id,
            )
            if result is not None:
                matched = 1
                detail = f"手动字幕: {target.folder_path}: {result.name}"
                self._log_progress(f"字幕已下载: {target.folder_path} -> {result.name}")
            else:
                detail = f"手动字幕: {target.folder_path}: 未找到可用字幕"
                self._log_progress(f"未找到可用字幕: {target.folder_path}")
            return result
        except Exception as exc:
            failed = 1
            detail = f"手动字幕: {target.folder_path}: {exc}"
            self._log_progress(f"字幕下载失败: {target.folder_path}: {exc}")
            raise
        finally:
            await conn.aclose()
            if log_id is not None:
                with self._session_factory.begin() as sess:
                    sub_log = sess.get(ScrapeLog, log_id)
                    if sub_log is not None:
                        sub_log.finished_at = datetime.now(UTC)
                        sub_log.total = 1
                        sub_log.matched = matched
                        sub_log.failed = failed
                        sub_log.detail = detail

    # ------------------------------------------------------------------
    # Batch subtitle refresh
    # ------------------------------------------------------------------

    async def _refresh_subtitles_impl(self) -> ScrapeLog:
        """Download subtitles for all matched items with non-Chinese titles.

        Skips items whose ``matched_original_title`` or ``matched_title`` is
        primarily Chinese (contains CJK characters), since those already have
        matching Chinese-language subtitles or don't need external ones.
        """
        import re

        _RE_CJK = re.compile(r"[一-鿿]")

        if not self._config.subtitle_enabled or self._subtitle is None:
            raise ScrapeError("字幕功能未启用，请在设置中开启")

        # Collect eligible items in a short transaction
        with self._session_factory() as sess:
            rows = sess.execute(
                select(MediaItem).where(
                    MediaItem.status == "matched",
                    MediaItem.matched_title.isnot(None),
                )
            ).scalars().all()

        eligible: list[MediaItem] = []
        for item in rows:
            # Prefer the original (usually English) title for the language check
            check_title = item.matched_original_title or item.matched_title or ""
            if not check_title:
                continue
            if _RE_CJK.search(check_title):
                continue  # Skip Chinese-titled items
            # Also require at least a poster URL or imdb_id to avoid scraping
            # items that won't get good subtitle matches
            eligible.append(item)

        total = len(eligible)
        matched = 0
        failed = 0
        detail_lines: list[str] = []

        log_id: int | None = None
        with self._session_factory.begin() as sess:
            log = ScrapeLog(
                started_at=datetime.now(UTC),
                total=total, matched=0, failed=0,
            )
            sess.add(log)
            sess.flush()
            log_id = log.id

        try:
            for index, item in enumerate(eligible):
                target = self._load_target(item.id)
                if target is None:
                    continue
                if self._stop_requested:
                    msg = "任务已手动停止"
                    remaining = eligible[index:]
                    detail_lines.extend(f"{it.folder_path}: {msg}" for it in remaining)
                    failed += len(remaining)
                    self._log_progress(msg)
                    break

                conn = _library_connection_from_target(target, self._enc_key)
                self._log_progress(f"正在下载字幕: {item.folder_path}")
                try:
                    title = item.matched_original_title or item.matched_title or ""
                    year = item.matched_year or item.parsed_year
                    result = await self._download_subtitle_for_target(
                        target, conn, title=title, year=year, imdb_id=item.imdb_id,
                    )
                    if result is not None:
                        matched += 1
                        detail_lines.append(
                            f"字幕已下载: {item.folder_path} -> {result.name}"
                        )
                        self._log_progress(f"字幕已下载: {item.folder_path}")
                    else:
                        failed += 1
                        detail_lines.append(f"未找到字幕: {item.folder_path}")
                        self._log_progress(f"未找到字幕: {item.folder_path}")
                except asyncio.CancelledError:
                    stop_msg = (
                        "任务已手动停止" if self._stop_requested else "任务因应用关闭而取消"
                    )
                    remaining = eligible[index:]
                    detail_lines.extend(
                        f"{it.folder_path}: {stop_msg}" for it in remaining
                    )
                    failed += len(remaining)
                    self._log_progress(stop_msg)
                    raise
                except Exception as exc:  # noqa: BLE001 (item isolation)
                    failed += 1
                    detail_lines.append(f"{item.folder_path}: {exc}")
                    self._log_progress(f"字幕下载失败: {item.folder_path}: {exc}")
                finally:
                    await conn.aclose()
        finally:
            if log_id is not None:
                with self._session_factory.begin() as sess:
                    existing_log = sess.get(ScrapeLog, log_id)
                    if existing_log is not None:
                        existing_log.finished_at = datetime.now(UTC)
                        existing_log.total = total
                        existing_log.matched = matched
                        existing_log.failed = failed
                        existing_log.detail = (
                            "\n".join(detail_lines) if detail_lines else None
                        )
            self._log_progress(
                f"字幕刷新完成: 总计 {total} / 已下载 {matched} / 未找到或失败 {failed}"
            )

        with self._session_factory() as sess:
            final_log = sess.get(ScrapeLog, log_id)
            if final_log is None:
                raise RuntimeError(f"ScrapeLog {log_id} not found after subtitle refresh")
            return final_log

    # ------------------------------------------------------------------
    # Core scrape logic
    # ------------------------------------------------------------------

    async def _scrape_one(
        self, target: ScrapeTarget, *, force: bool, conn: Connection,
        tmdb_id: int | None = None, query: str | None = None,
    ) -> ScrapeResult:
        """Scrape a single item.  ``force`` bypasses NFO skip.

        ``tmdb_id`` forces a specific TMDB id (manual match); ``query`` searches
        that query instead of the parsed title.  Raises on failure; the caller
        persists the result or marks failed.
        """
        rel_folder = _relative_folder(target.folder_path, target.library_path)

        # Step 1: NFO skip check
        if (
            not force
            and not self._config.overwrite_existing_nfo
            and target.status == "pending"
            and await _nfo_exists_async(conn, rel_folder, target.media_type)
        ):
            return ExistingNfoMatched()

        # Step 2: must have a title (unless a manual override is provided)
        if tmdb_id is None and not query and not target.parsed_title:
            raise ScrapeError("标题解析为空，无法搜索")

        # Step 3: TMDB search + detail
        if tmdb_id is not None:
            meta = await self._tmdb.fetch_by_id(tmdb_id, target.media_type)
        else:
            search_title = query if query else target.parsed_title
            if not search_title:
                raise ScrapeError("标题解析为空，无法搜索")
            found = await self._tmdb.search_and_fetch(
                search_title,
                None if query else target.parsed_year,
                target.media_type,
            )
            if found is None:
                raise ScrapeError("TMDB 无搜索结果")
            meta = found

        # Step 4: Douban supplement
        if self._config.use_douban and self._douban:
            try:
                supp = await self._douban.fetch_supplement(
                    meta.title, meta.year,
                )
            except Exception:
                logger.warning(
                    "豆瓣模块异常(%s)", meta.title, exc_info=True,
                )
                supp = None

            if supp is not None:
                if supp.overview is not None:
                    meta.overview = supp.overview
                if supp.rating is not None:
                    meta.rating = supp.rating

        # Step 5: Download images
        if meta.poster_url:
            poster_bytes = await self._tmdb.fetch_image(meta.poster_url)
            await conn.write_bytes(_image_rel(rel_folder, "poster"), poster_bytes)
        if meta.backdrop_url:
            backdrop_bytes = await self._tmdb.fetch_image(meta.backdrop_url)
            await conn.write_bytes(_image_rel(rel_folder, "fanart"), backdrop_bytes)

        # Step 6: Write NFO (last — completion marker before subtitles)
        nfo_rel = _nfo_rel(rel_folder, target.media_type)
        nfo_bytes = (
            build_movie_nfo_bytes(meta)
            if target.media_type == "movie"
            else build_tvshow_nfo_bytes(meta)
        )
        await conn.write_bytes(nfo_rel, nfo_bytes)

        # Step 7: Subtitle download (best-effort, after NFO).  Prefer the
        # original (usually English) title + imdb_id - subtitle sites match far
        # better on those than on the localized title.
        if self._config.subtitle_enabled and self._subtitle is not None:
            self._log_progress(f"正在刮削字幕: {target.folder_path}")
            try:
                sub = await self._download_subtitle_for_target(
                    target, conn,
                    title=meta.original_title or meta.title,
                    year=meta.year,
                    imdb_id=meta.imdb_id,
                )
                if sub is not None:
                    self._log_progress(f"字幕已下载: {target.folder_path} -> {sub.name}")
                else:
                    self._log_progress(f"未找到可用字幕: {target.folder_path}")
            except Exception as exc:
                self._log_progress(f"字幕下载失败: {target.folder_path}: {exc}")
                logger.warning(
                    "Subtitle download failed for %s", target.parsed_title, exc_info=True,
                )

        return meta

    # ------------------------------------------------------------------
    # Subtitle download (shared by auto-scrape and manual trigger)
    # ------------------------------------------------------------------

    async def _download_subtitle_for_target(
        self,
        target: ScrapeTarget,
        conn: Connection,
        *,
        title: str,
        year: int | None,
        imdb_id: str | None = None,
    ) -> Path | None:
        """Download subtitles for *target*; returns the saved path or ``None``.

        Shared by the auto-scrape subtitle step and the manual per-item
        ``download_subtitle`` entry point.  ``video_filename`` must be relative
        to ``media_folder`` so local and remote (connection-backed) writes land
        next to the video file.
        """
        if not self._subtitle:
            return None
        rel_folder = _relative_folder(target.folder_path, target.library_path)
        if target.is_file:
            media_folder = Path(target.folder_path).parent
            video_filename = PurePosixPath(rel_folder).name
        else:
            media_folder = Path(target.folder_path)
            video_rel = await _find_video_file_async(conn, rel_folder)
            if not video_rel:
                return None
            video_filename = str(
                PurePosixPath(video_rel).relative_to(PurePosixPath(rel_folder))
            )
        return await self._subtitle.download(
            title=title,
            year=year,
            media_folder=media_folder,
            video_filename=video_filename,
            imdb_id=imdb_id,
            connection=conn,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _load_target(self, item_id: int) -> ScrapeTarget | None:
        """Load a MediaItem as an immutable ScrapeTarget, closing session immediately."""
        with self._session_factory() as sess:
            item = sess.get(MediaItem, item_id)
            if item is None:
                return None
            lib = sess.get(Library, item.library_id)
            if lib is None:
                return None
            return ScrapeTarget(
                id=item.id,
                library_id=item.library_id,
                library_path=lib.path,
                connection_type=lib.connection_type or "local",
                connection_config_encrypted=lib.connection_config_encrypted,
                folder_path=item.folder_path,
                media_type=item.media_type,
                parsed_title=item.parsed_title,
                parsed_year=item.parsed_year,
                status=item.status,
                is_file=Path(item.folder_path).suffix.lower() in VIDEO_EXTENSIONS,
            )


# ---------------------------------------------------------------------------
# Internal: DB helpers used by ScanRunner
# ---------------------------------------------------------------------------


def _apply_nfo_meta(item: MediaItem, meta: dict[str, object], now: datetime) -> None:
    """Populate a MediaItem's matched-* fields from a parsed local NFO.

    Marks the item ``matched`` with ``source="nfo"`` so it is never re-scraped
    (the bundled NFO is treated as authoritative). Only call when an NFO exists.
    """
    if meta.get("title"):
        item.matched_title = str(meta["title"])
    if meta.get("originaltitle"):
        item.matched_original_title = str(meta["originaltitle"])
    if meta.get("year"):
        try:
            item.matched_year = int(str(meta["year"]))
        except (ValueError, TypeError):
            pass
    if meta.get("rating") is not None:
        try:
            item.rating = float(meta["rating"])  # type: ignore[arg-type]
        except (ValueError, TypeError):
            pass
    if meta.get("plot"):
        item.overview = str(meta["plot"])
    genres = meta.get("genres")
    if (
        isinstance(genres, Iterable)
        and not isinstance(genres, (str, bytes, dict))
    ):
        item.genres = ",".join(str(genre) for genre in genres)
    if meta.get("tmdb_id"):
        item.source_id = str(meta["tmdb_id"])
    if meta.get("imdb_id"):
        item.imdb_id = str(meta["imdb_id"])
    item.source = "nfo"
    item.status = "matched"
    item.error_message = None
    item.last_scraped_at = now


def _upsert_item(
    sess: Session,
    lib: Library,
    folder_path: str,
    overwrite: bool,
    *,
    has_nfo: bool = False,
    nfo_meta: dict[str, object] | None = None,
) -> MediaItem | None:
    """Insert or update a MediaItem for a discovered folder.

    Follows the state-machine table (§9.1). When an existing NFO is present
    and parseable, its metadata is loaded into the item (source="nfo") so the
    folder is never sent to TMDB.
    """
    parsed: ParsedName = parse_folder_name(Path(folder_path).name)

    # Check for existing record
    existing = sess.execute(
        select(MediaItem).where(MediaItem.folder_path == folder_path)
    ).scalar_one_or_none()

    if existing is not None:
        return _update_existing(
            sess, existing, lib, parsed, overwrite,
            has_nfo=has_nfo, nfo_meta=nfo_meta,
        )

    # New record
    return _create_new(
        sess, lib, folder_path, parsed, overwrite,
        has_nfo=has_nfo, nfo_meta=nfo_meta,
    )


def _create_new(
    sess: Session,
    lib: Library,
    folder_path: str,
    parsed: ParsedName,
    overwrite: bool,
    *,
    has_nfo: bool = False,
    nfo_meta: dict[str, object] | None = None,
) -> MediaItem:
    """Create a new MediaItem from a newly discovered folder."""
    if has_nfo and not overwrite:
        # NFO exists → enters queue, will be skipped as ExistingNfoMatched
        status = "pending"
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
    # A folder that ships a parseable NFO is loaded from it directly and never
    # scraped (the bundled NFO is authoritative).
    if has_nfo and not overwrite and nfo_meta:
        _apply_nfo_meta(item, nfo_meta, datetime.now(UTC))
    return item


def _update_existing(
    sess: Session,
    item: MediaItem,
    lib: Library,
    parsed: ParsedName,
    overwrite: bool,
    *,
    has_nfo: bool = False,
    nfo_meta: dict[str, object] | None = None,
) -> MediaItem:
    """Update an existing MediaItem's parsed fields and status."""
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

    # If the folder ships a parseable NFO and we have no real metadata yet,
    # load it now (covers new scans and previously-empty ExistingNfoMatched
    # items). Items already scraped (matched_title set) are left untouched so
    # their live metadata is never clobbered by an NFO re-read.
    if has_nfo and not overwrite and nfo_meta and not item.matched_title:
        _apply_nfo_meta(item, nfo_meta, datetime.now(UTC))

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


# ---------------------------------------------------------------------------
# Ignored (deleted) item paths
# ---------------------------------------------------------------------------

_IGNORED_META_KEY = "ignored_paths"


def _ignored_paths(sess: Session) -> set[str]:
    """Return the set of normalized paths the user deleted (record-only)."""
    meta = sess.get(AppMeta, _IGNORED_META_KEY)
    if meta is None:
        return set()
    return {p for p in meta.value.splitlines() if p}


def _set_ignored_paths(sess: Session, paths: set[str]) -> None:
    """Persist the ignored-path set under AppMeta (newline-separated)."""
    meta = sess.get(AppMeta, _IGNORED_META_KEY)
    if meta is None:
        meta = AppMeta(key=_IGNORED_META_KEY, value="")
        sess.add(meta)
    meta.value = "\n".join(sorted(paths))


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
    item.imdb_id = result.imdb_id
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
