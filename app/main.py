"""FastAPI application and Web layer (M9).

Provides server-rendered Jinja2 pages and form-based POST routes.
See implementation spec §11 for route contracts.
"""

from __future__ import annotations

import asyncio
import logging
import math
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote, unquote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape
from sqlalchemy import func, select

from app import VIDEO_EXTENSIONS, get_data_dir
from app.config import AppConfig, load_config, save_config, validate_cron
from app.crypto import decrypt_dict, load_or_create_key
from app.database import (
    AppMeta,
    Library,
    MediaItem,
    ScrapeLog,
    create_session_factory,
    init_db,
)
from app.exceptions import (
    ConfigError,
    ItemNotFoundError,
    ScanBusyError,
    ScrapeError,
)
from app.scanner import (
    ScanRunner,
    _detect_subtitle_summary_async,
    _ignored_paths,
    _library_connection,
    _relative_folder,
    _set_ignored_paths,
    normalize_path,
)
from app.scheduler import ScrapeScheduler
from app.scrapers.douban import DoubanScraper
from app.scrapers.subtitle import SubtitleDownloader
from app.scrapers.tmdb import TmdbScraper

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_TEMPLATES_DIR = Path(__file__).parent / "templates"
_STATIC_DIR = Path(__file__).parent / "static"

# ---------------------------------------------------------------------------
# App factory
# ---------------------------------------------------------------------------


def create_app(
    data_dir: Path | None = None,
    start_scheduler: bool = True,
) -> FastAPI:
    """Create and wire the FastAPI application.

    Args:
        data_dir: Override data directory (used in tests).
        start_scheduler: If False, the scheduler is not started (test mode).
    """
    if data_dir is None:
        data_dir = get_data_dir()

    jinja_env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    jinja_env.filters["localtime"] = _filter_localtime
    jinja_env.filters["mask_key"] = _filter_mask_key

    def _render(name: str, context: dict[str, Any]) -> HTMLResponse:
        """Render a Jinja2 template and return an HTMLResponse."""
        template = jinja_env.get_template(name)
        return HTMLResponse(template.render(**context))

    # ------------------------------------------------------------------
    # Lifespan
    # ------------------------------------------------------------------

    @asynccontextmanager
    async def lifespan(app: FastAPI):  # type: ignore[no-untyped-def]
        # Bootstrap
        data_dir.mkdir(parents=True, exist_ok=True)
        config_path = data_dir / "config.yaml"
        db_path = data_dir / "tmm-lite.db"

        config = load_config(config_path)
        engine = init_db(db_path)
        session_factory = create_session_factory(engine)

        # Seed import (one-time)
        _run_seed_import(session_factory, config)

        # Scrapers
        tmdb = TmdbScraper(
            config.effective_tmdb_api_key, config.language, config.proxy,
            min_interval=config.tmdb_delay_seconds,
        )
        douban = DoubanScraper(config.douban_delay_seconds) if config.use_douban else None

        # Encryption key (for connection credentials)
        enc_key = load_or_create_key(data_dir)

        # Runner
        runner = ScanRunner(session_factory, config, tmdb, douban, enc_key=enc_key)

        # Subtitle downloader
        subtitle_dl: SubtitleDownloader | None = None
        if config.subtitle_enabled:
            subtitle_dl = SubtitleDownloader(
                opensubtitles_api_key=config.opensubtitles_api_key,
                preferred_languages=config.subtitle_languages,
            )
        runner.set_subtitle_downloader(subtitle_dl)

        # Scheduler
        scheduler = ScrapeScheduler(runner)
        if start_scheduler:
            scheduler.start(config.schedule_cron)
            if not config.scheduler_enabled:
                scheduler.pause()

        # Store on app state
        app.state.config = config
        app.state.enc_key = enc_key
        app.state.session_factory = session_factory
        app.state.runner = runner
        app.state.scheduler = scheduler
        app.state.tmdb = tmdb
        app.state.douban = douban
        app.state.subtitle_dl = subtitle_dl
        app.state.config_path = config_path
        app.state.settings_lock = asyncio.Lock()

        try:
            yield
        finally:
            # Graceful shutdown
            scheduler.pause()
            await runner.shutdown()
            await scheduler.shutdown()
            if subtitle_dl is not None:
                await subtitle_dl.aclose()
            await tmdb.aclose()
            if douban is not None:
                await douban.aclose()
            engine.dispose()

    app = FastAPI(lifespan=lifespan)

    # Static files
    if _STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

    # ------------------------------------------------------------------
    # Route helpers
    # ------------------------------------------------------------------

    def _redirect(path: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
        params: list[str] = []
        if ok:
            params.append(f"ok={quote(ok)}")
        if err:
            params.append(f"err={quote(err)}")
        qs = "&".join(params)
        url = path + ("?" + qs if qs else "")
        return RedirectResponse(url, status_code=303)

    def _now_local() -> datetime:
        return datetime.now(UTC)

    # ------------------------------------------------------------------
    # GET /
    # ------------------------------------------------------------------

    @app.get("/")
    async def dashboard(request: Request) -> Any:
        sess = request.app.state.session_factory()
        try:
            library_count = sess.execute(
                select(func.count(Library.id))
            ).scalar_one()

            counts = {}
            for status in ("pending", "matched", "failed", "manual_needed", "missing"):
                counts[status] = sess.execute(
                    select(func.count(MediaItem.id)).where(
                        MediaItem.status == status
                    )
                ).scalar_one()

            runner: ScanRunner = request.app.state.runner
            scheduler: ScrapeScheduler = request.app.state.scheduler

            last_log = sess.execute(
                select(ScrapeLog).order_by(ScrapeLog.id.desc()).limit(1)
            ).scalar_one_or_none()

            config: AppConfig = request.app.state.config
            key_warning = (
                "未配置 TMDB API Key，需要联网刮削的条目将失败，请到设置页填写"
                if not config.effective_tmdb_api_key else ""
            )

            return _render(
                "dashboard.html",
                {
                    "request": request,
                    "library_count": library_count,
                    "counts": counts,
                    "is_running": runner.is_running,
                    "next_run_time": _format_time(scheduler.next_run_time),
                    "scheduler_paused": scheduler.paused,
                    "last_log": last_log,
                    "key_warning": key_warning,
                    "ok": unquote(request.query_params.get("ok", "")),
                    "err": unquote(request.query_params.get("err", "")),
                },
            )
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # POST /run-scrape
    # ------------------------------------------------------------------

    @app.post("/run-scrape")
    async def run_scrape(request: Request) -> Any:
        runner: ScanRunner = request.app.state.runner
        try:
            runner.start_full_background()
        except ScanBusyError:
            return _redirect("/", err="任务正在运行中，请稍后")
        return _redirect("/", ok="任务已启动")

    # ------------------------------------------------------------------
    # POST /stop-scrape
    # ------------------------------------------------------------------

    @app.post("/stop-scrape")
    async def stop_scrape(request: Request) -> Any:
        """Request a graceful stop of the running scan (if any)."""
        runner: ScanRunner = request.app.state.runner
        if runner.stop():
            return _redirect("/", ok="已请求停止，剩余条目将标记为已取消")
        return _redirect("/", err="当前没有正在运行的任务")

    # ------------------------------------------------------------------
    # POST /rescrape-failed
    # ------------------------------------------------------------------

    @app.post("/rescrape-failed")
    async def rescrape_failed(request: Request) -> Any:
        """Re-scrape all failed items in the background (rate-limited)."""
        runner: ScanRunner = request.app.state.runner
        sess = request.app.state.session_factory()
        try:
            failed_count = sess.execute(
                select(func.count(MediaItem.id)).where(MediaItem.status == "failed")
            ).scalar_one()
        finally:
            sess.close()

        if failed_count == 0:
            return _redirect("/", err="没有失败的条目需要重新刮削")
        try:
            runner.start_rescrape_failed_background()
        except ScanBusyError:
            return _redirect("/", err="任务正在运行中，请稍后")
        return _redirect("/", ok=f"已开始重新刮削 {failed_count} 条失败条目")

    # ------------------------------------------------------------------
    # GET /libraries
    # ------------------------------------------------------------------

    @app.get("/libraries")
    async def libraries_list(request: Request) -> Any:
        sess = request.app.state.session_factory()
        try:
            libs = sess.execute(
                select(Library).order_by(Library.id)
            ).scalars().all()

            lib_data: list[dict[str, object]] = []
            for lib in libs:
                count = sess.execute(
                    select(func.count(MediaItem.id)).where(
                        MediaItem.library_id == lib.id
                    )
                ).scalar_one()
                lib_data.append({
                    "id": lib.id,
                    "name": lib.name,
                    "path": lib.path,
                    "media_type": lib.media_type,
                    "connection_type": lib.connection_type or "local",
                    "item_count": count,
                })

            # Build a list of reusable remote connections (deduped by
            # type+host+port+user) so the add form can offer "select an
            # existing connection". Passwords are NEVER sent to the browser;
            # browse/test resolve credentials server-side by library id.
            saved_connections: list[dict[str, object]] = []
            seen: set[tuple[str, str, int, str]] = set()
            enc_key = request.app.state.enc_key
            for lib in libs:
                ct = lib.connection_type or "local"
                if ct == "local" or not lib.connection_config_encrypted:
                    continue
                try:
                    cfg = decrypt_dict(lib.connection_config_encrypted, enc_key)
                except Exception:  # noqa: BLE001
                    continue
                host = str(cfg.get("host", "")).strip()
                username = str(cfg.get("username", "")).strip()
                try:
                    port_i = int(cfg.get("port") or 0)
                except (TypeError, ValueError):
                    port_i = 0
                if not host:
                    continue
                dedup = (ct, host.lower(), port_i, username.lower())
                if dedup in seen:
                    continue
                seen.add(dedup)
                port_part = f":{port_i}" if port_i else ""
                saved_connections.append(
                    {
                        "lib_id": lib.id,
                        "type": ct,
                        "label": f"{username or '?'}@{host}{port_part}",
                    }
                )

            return _render(
                "libraries.html",
                {
                    "request": request,
                    "libraries": lib_data,
                    "saved_connections": saved_connections,
                    "is_running": request.app.state.runner.is_running,
                    "ok": unquote(request.query_params.get("ok", "")),
                    "err": unquote(request.query_params.get("err", "")),
                },
            )
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # POST /libraries/add
    # ------------------------------------------------------------------

    @app.post("/libraries/add")
    async def libraries_add(
        request: Request,
        name: str = Form(""),
        path: str = Form(""),
        media_type: str = Form(""),
        connection_type: str = Form("local"),
        conn_host: str = Form(""),
        conn_port: str = Form(""),
        conn_username: str = Form(""),
        conn_password: str = Form(""),
        source_lib_id: str = Form(""),
    ) -> Any:
        # Adding a library is just a DB row insert; allow it even while a
        # scan runs (the new library is picked up on the next scan/rescan).
        # Only re-scan itself is mutually exclusive.

        # Validate
        if not name.strip():
            return _redirect("/libraries", err="名称不能为空")
        if not path.strip():
            return _redirect("/libraries", err="路径不能为空")
        if not path.startswith("/"):
            return _redirect("/libraries", err="路径必须是绝对路径")
        if media_type not in ("movie", "tv"):
            return _redirect("/libraries", err="类型无效")
        if connection_type not in ("local", "ssh", "webdav", "smb"):
            return _redirect("/libraries", err="连接方式无效")

        norm_path = normalize_path(path)

        sess = request.app.state.session_factory()
        try:
            existing = sess.execute(
                select(Library).where(Library.path == norm_path)
            ).scalar_one_or_none()
            if existing is not None:
                return _redirect("/libraries", err="该路径已存在")

            # Connection config: either reuse an existing library's
            # connection (copy its encrypted config verbatim so the new
            # library is self-contained) or encrypt freshly typed creds.
            enc_config: str | None = None
            if connection_type != "local":
                if source_lib_id.strip():
                    try:
                        src = sess.get(Library, int(source_lib_id.strip()))
                    except (TypeError, ValueError):
                        src = None
                    if (
                        src is None
                        or src.connection_type == "local"
                        or not src.connection_config_encrypted
                    ):
                        return _redirect("/libraries", err="所选连接不可用，请重新选择")
                    connection_type = src.connection_type
                    enc_config = src.connection_config_encrypted
                else:
                    import json

                    from app.crypto import encrypt_str
                    try:
                        port = int(conn_port) if conn_port.strip() else (22 if connection_type == "ssh" else 443)
                    except ValueError:
                        return _redirect("/libraries", err="端口必须是数字")
                    cfg = {
                        "host": conn_host.strip(),
                        "port": port,
                        "username": conn_username.strip(),
                        "password": conn_password,
                    }
                    enc_config = encrypt_str(json.dumps(cfg), request.app.state.enc_key)

            lib = Library(
                name=name.strip(),
                path=norm_path,
                media_type=media_type,
                connection_type=connection_type,
                connection_config_encrypted=enc_config,
            )
            sess.add(lib)
            sess.commit()

            ok_msg = "已添加"
            if connection_type == "local" and not Path(norm_path).exists():
                ok_msg += "（注意：当前容器内看不到该路径）"
        finally:
            sess.close()

        return _redirect("/libraries", ok=ok_msg)

    # ------------------------------------------------------------------
    # POST /libraries/{id}/rescan
    # ------------------------------------------------------------------

    @app.post("/libraries/{lib_id}/rescan")
    async def libraries_rescan(request: Request, lib_id: int) -> Any:
        """Re-scan a single library in the background."""
        runner: ScanRunner = request.app.state.runner
        sess = request.app.state.session_factory()
        try:
            lib = sess.get(Library, lib_id)
            if lib is None:
                return JSONResponse({"detail": "library not found"}, status_code=404)
            lib_name = lib.name
        finally:
            sess.close()

        try:
            runner.start_rescan_library_background(lib_id)
        except ScanBusyError:
            return _redirect("/libraries", err="任务正在运行中，请稍后")

        return _redirect("/libraries", ok=f"已开始重新扫描库: {lib_name}")

    # ------------------------------------------------------------------
    # POST /libraries/{id}/delete
    # ------------------------------------------------------------------

    @app.post("/libraries/{lib_id}/delete")
    async def libraries_delete(request: Request, lib_id: int) -> Any:
        # Deleting a library is allowed while a scan runs. If the deleted
        # library happens to be the one currently being scanned, per-item
        # inserts may log FK errors, but the scan's try/finally still
        # releases the run lock — no crash, no stuck state.

        sess = request.app.state.session_factory()
        try:
            lib = sess.get(Library, lib_id)
            if lib is None:
                return JSONResponse({"detail": "library not found"}, status_code=404)
            sess.delete(lib)
            sess.commit()
        finally:
            sess.close()

        return _redirect("/libraries", ok="已删除媒体库及其条目记录（磁盘文件未动）")

    # ------------------------------------------------------------------
    # GET /items
    # ------------------------------------------------------------------

    @app.get("/items")
    async def items_list(request: Request, status: str = "") -> Any:
        sess = request.app.state.session_factory()
        try:
            stmt = select(MediaItem).order_by(MediaItem.id)
            if status in ("pending", "matched", "failed", "manual_needed", "missing"):
                stmt = stmt.where(MediaItem.status == status)

            items = sess.execute(stmt).scalars().all()

            # Count per status for tabs
            tab_counts: dict[str, int] = {}
            for s in ("pending", "matched", "failed", "manual_needed", "missing"):
                tab_counts[s] = sess.execute(
                    select(func.count(MediaItem.id)).where(MediaItem.status == s)
                ).scalar_one()

            ignored_count = len(_ignored_paths(sess))

            # Subtitle status per item — one connection per library to avoid
            # repeated handshakes, then list each item's folder.
            subtitle_map: dict[int, str | None] = {}
            items_by_lib: dict[int, list[MediaItem]] = {}
            for it in items:
                items_by_lib.setdefault(it.library_id, []).append(it)
            for lib_id, lib_items in items_by_lib.items():
                lib = sess.get(Library, lib_id)
                if lib is None:
                    continue
                conn = _library_connection(lib, request.app.state.enc_key)
                try:
                    for it in lib_items:
                        rel = _relative_folder(it.folder_path, lib.path)
                        subtitle_map[it.id] = await _detect_subtitle_summary_async(conn, rel)
                except Exception:
                    logger.warning("字幕检测失败(lib=%s)", lib.name, exc_info=True)
                finally:
                    await conn.aclose()

            return _render(
                "items.html",
                {
                    "request": request,
                    "items": items,
                    "subtitle_map": subtitle_map,
                    "current_status": status,
                    "tab_counts": tab_counts,
                    "ignored_count": ignored_count,
                    "is_running": request.app.state.runner.is_running,
                    "ok": unquote(request.query_params.get("ok", "")),
                    "err": unquote(request.query_params.get("err", "")),
                },
            )
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # POST /items/{id}/rescrape
    # ------------------------------------------------------------------

    @app.post("/items/{item_id}/rescrape")
    async def items_rescrape(request: Request, item_id: int) -> Any:
        runner: ScanRunner = request.app.state.runner
        form = await request.form()
        query = str(form.get("query", "")).strip()
        tmdb_id_str = str(form.get("tmdb_id", "")).strip()
        tmdb_id = int(tmdb_id_str) if tmdb_id_str.isdigit() else None
        background = form.get("background") == "1"

        try:
            if background:
                runner.start_rescrape_item_background(
                    item_id, query=query or None, tmdb_id=tmdb_id,
                )
                return _redirect(
                    "/items",
                    ok="已开始后台重新刮削，稍后刷新查看结果",
                )
            result = await runner.rescrape_item(
                item_id, query=query or None, tmdb_id=tmdb_id,
            )
            return _redirect(
                "/items",
                ok=f"已完成重新刮削: {result.status}",
            )
        except ScanBusyError:
            return _redirect("/items", err="任务正在运行中")
        except ItemNotFoundError:
            return JSONResponse({"detail": "item not found"}, status_code=404)

    # ------------------------------------------------------------------
    # GET /api/search — TMDB candidates for the manual-match dialog
    # ------------------------------------------------------------------

    @app.get("/api/search")
    async def api_search(
        request: Request, title: str = "", media_type: str = "movie",
    ) -> Any:
        if not title.strip():
            return {"items": []}
        if media_type not in ("movie", "tv"):
            return JSONResponse({"error": "media_type 无效", "items": []}, status_code=400)
        tmdb = request.app.state.tmdb
        try:
            items = await tmdb.search_candidates(title.strip(), media_type)
        except Exception as exc:  # noqa: BLE001 (search is best-effort)
            return JSONResponse({"error": str(exc), "items": []}, status_code=500)
        return {"items": items}

    # ------------------------------------------------------------------
    # POST /items/{id}/subtitle
    # ------------------------------------------------------------------

    @app.post("/items/{item_id}/subtitle")
    async def items_subtitle(request: Request, item_id: int) -> Any:
        runner: ScanRunner = request.app.state.runner
        try:
            result = await runner.download_subtitle(item_id)
            if result is not None:
                return _redirect("/items", ok=f"字幕已下载: {result.name}")
            return _redirect("/items", err="未找到可用的字幕")
        except ScanBusyError:
            return _redirect("/items", err="任务正在运行中")
        except ItemNotFoundError:
            return JSONResponse({"detail": "item not found"}, status_code=404)
        except ScrapeError as exc:
            return _redirect("/items", err=str(exc))

    # ------------------------------------------------------------------
    # POST /items/refresh-subtitles
    # ------------------------------------------------------------------

    @app.post("/items/refresh-subtitles")
    async def items_refresh_subtitles(request: Request) -> Any:
        """Batch download subtitles for all matched items with non-Chinese titles."""
        runner: ScanRunner = request.app.state.runner
        config: AppConfig = request.app.state.config

        if not config.subtitle_enabled:
            return _redirect("/items", err="字幕功能未启用，请在设置中开启")

        sess = request.app.state.session_factory()
        try:
            import re
            _RE_CJK = re.compile(r"[一-鿿]")
            rows = sess.execute(
                select(MediaItem).where(
                    MediaItem.status == "matched",
                    MediaItem.matched_title.isnot(None),
                )
            ).scalars().all()

            eligible_count = 0
            for item in rows:
                check_title = item.matched_original_title or item.matched_title or ""
                if check_title and not _RE_CJK.search(check_title):
                    eligible_count += 1
        finally:
            sess.close()

        if eligible_count == 0:
            return _redirect("/items", err="没有可刷新字幕的条目（全部已经是中文标题）")

        try:
            runner.start_refresh_subtitles_background()
        except ScanBusyError:
            return _redirect("/items", err="任务正在运行中，请稍后")

        return _redirect("/items", ok=f"已开始刷新字幕，共 {eligible_count} 条非中文标题条目")

    # ------------------------------------------------------------------
    # POST /items/{id}/delete
    # ------------------------------------------------------------------

    @app.post("/items/{item_id}/delete")
    async def items_delete(request: Request, item_id: int) -> Any:
        """Delete a MediaItem record only — disk files are untouched.

        The path is also recorded as ignored so a later scan does not re-add it.
        """
        runner: ScanRunner = request.app.state.runner
        if runner.is_running:
            return _redirect("/items", err="任务正在运行中，暂不能删除记录")

        sess = request.app.state.session_factory()
        try:
            item = sess.get(MediaItem, item_id)
            if item is None:
                return JSONResponse({"detail": "item not found"}, status_code=404)
            ignored = _ignored_paths(sess)
            ignored.add(normalize_path(item.folder_path))
            _set_ignored_paths(sess, ignored)
            sess.delete(item)
            sess.commit()
            return _redirect("/items", ok="已删除该记录（磁盘文件未动）")
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # POST /items/clear-ignored
    # ------------------------------------------------------------------

    @app.post("/items/clear-ignored")
    async def items_clear_ignored(request: Request) -> Any:
        """Clear the ignored-path list so deleted items re-appear on rescan."""
        runner: ScanRunner = request.app.state.runner
        if runner.is_running:
            return _redirect("/items", err="任务正在运行中，暂不能修改忽略列表")
        sess = request.app.state.session_factory()
        try:
            _set_ignored_paths(sess, set())
            sess.commit()
            return _redirect("/items", ok="已清空忽略列表")
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # GET /logs
    # ------------------------------------------------------------------

    @app.get("/logs")
    async def logs_list(request: Request) -> Any:
        sess = request.app.state.session_factory()
        try:
            logs = sess.execute(
                select(ScrapeLog).order_by(ScrapeLog.id.desc()).limit(50)
            ).scalars().all()

            return _render(
                "logs.html",
                {
                    "request": request,
                    "logs": logs,
                },
            )
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # GET /scan-live — real-time progress of the currently running scan
    # ------------------------------------------------------------------

    @app.get("/scan-live")
    async def scan_live(request: Request) -> Any:
        runner: ScanRunner = request.app.state.runner
        return _render(
            "scan_live.html",
            {
                "request": request,
                "is_running": runner.is_running,
                "lines": runner.progress_lines(),
            },
        )

    # ------------------------------------------------------------------
    # GET /settings
    # ------------------------------------------------------------------

    @app.get("/settings")
    async def settings_page(request: Request) -> Any:
        config: AppConfig = request.app.state.config
        scheduler: ScrapeScheduler = request.app.state.scheduler
        key_placeholder = (
            f"已设置(****{config.tmdb_api_key[-4:]})，留空表示不修改"
            if config.tmdb_api_key else "未设置"
        )

        return _render(
            "settings.html",
            {
                "request": request,
                "config": config,
                "key_placeholder": key_placeholder,
                "next_run_time": _format_time(scheduler.next_run_time),
                "ok": unquote(request.query_params.get("ok", "")),
                "err": unquote(request.query_params.get("err", "")),
            },
        )

    # ------------------------------------------------------------------
    # POST /settings
    # ------------------------------------------------------------------

    @app.post("/settings")
    async def settings_save(request: Request) -> Any:
        lock: asyncio.Lock = request.app.state.settings_lock

        # Collect form data (cast to str — no file uploads in settings)
        form = await request.form()
        tmdb_key = str(form.get("tmdb_api_key", ""))
        clear_key = form.get("clear_tmdb_api_key") == "on"
        use_douban = form.get("use_douban") == "on"
        delay_str = str(form.get("douban_delay_seconds", "2.0"))
        tmdb_delay_str = str(form.get("tmdb_delay_seconds", "0.5"))
        overwrite = form.get("overwrite_existing_nfo") == "on"
        cron_str = str(form.get("schedule_cron", "0 4 * * *"))
        scheduler_enabled = form.get("scheduler_enabled") == "on"
        subtitle_enabled = form.get("subtitle_enabled") == "on"
        os_api_key = str(form.get("opensubtitles_api_key", ""))
        subtitle_langs = str(form.get("subtitle_languages", "chi,zho,zh"))
        browse_root = str(form.get("browse_root", "/")).strip() or "/"
        proxy = str(form.get("proxy", "")).strip()

        async with lock:
            runner: ScanRunner = request.app.state.runner
            if runner.is_running:
                return _redirect("/settings", err="任务正在运行中，暂不能修改设置")

            # Validate cron first
            try:
                validate_cron(cron_str)
            except ConfigError:
                return _redirect("/settings", err="Cron 表达式无效，其余修改未保存")

            # Validate delay
            try:
                delay = float(delay_str)
            except (ValueError, TypeError):
                return _redirect("/settings", err="豆瓣请求间隔必须是数字")
            if not math.isfinite(delay) or delay < 0.5:
                return _redirect("/settings", err="豆瓣请求间隔必须 >= 0.5 秒")

            # Validate TMDB request interval
            try:
                tmdb_delay = float(tmdb_delay_str)
            except (ValueError, TypeError):
                return _redirect("/settings", err="TMDB 请求间隔必须是数字")
            if not math.isfinite(tmdb_delay) or tmdb_delay < 0:
                return _redirect("/settings", err="TMDB 请求间隔必须 >= 0 秒")

            # Build updates — empty password fields mean "don't change existing key"
            updates: dict[str, object] = {
                "use_douban": use_douban,
                "douban_delay_seconds": delay,
                "tmdb_delay_seconds": tmdb_delay,
                "overwrite_existing_nfo": overwrite,
                "schedule_cron": cron_str,
                "scheduler_enabled": scheduler_enabled,
                "subtitle_enabled": subtitle_enabled,
                "subtitle_languages": subtitle_langs.strip(),
                "browse_root": browse_root,
                "proxy": proxy,
            }
            if clear_key:
                updates["tmdb_api_key"] = ""
            elif tmdb_key.strip():
                updates["tmdb_api_key"] = tmdb_key.strip()
            if os_api_key.strip():
                updates["opensubtitles_api_key"] = os_api_key.strip()

            # Save old state for rollback
            old_config: AppConfig = request.app.state.config
            old_cron = old_config.schedule_cron

            try:
                new_config = save_config(updates, request.app.state.config_path)
            except ConfigError as exc:
                return _redirect("/settings", err=str(exc))

            # Prepare new scrapers
            new_tmdb = TmdbScraper(
                new_config.effective_tmdb_api_key, new_config.language, new_config.proxy,
                min_interval=new_config.tmdb_delay_seconds,
            )
            new_douban = DoubanScraper(new_config.douban_delay_seconds) if new_config.use_douban else None

            # Apply to running objects (commit point)
            try:
                request.app.state.scheduler.reschedule(cron_str)
                if new_config.scheduler_enabled:
                    request.app.state.scheduler.resume()
                else:
                    request.app.state.scheduler.pause()
                old_tmdb, old_douban = runner.reconfigure(new_config, new_tmdb, new_douban)

                # Recreate subtitle downloader
                old_sub = request.app.state.subtitle_dl
                new_sub: SubtitleDownloader | None = None
                if new_config.subtitle_enabled:
                    new_sub = SubtitleDownloader(
                        opensubtitles_api_key=new_config.opensubtitles_api_key,
                        preferred_languages=new_config.subtitle_languages,
                    )
                runner.set_subtitle_downloader(new_sub)
                request.app.state.subtitle_dl = new_sub
            except Exception:  # noqa: BLE001 (rollback on any failure)
                # Rollback: restore old config file
                rollback: dict[str, object] = {
                    "tmdb_api_key": old_config.tmdb_api_key,
                    "use_douban": old_config.use_douban,
                    "douban_delay_seconds": old_config.douban_delay_seconds,
                    "tmdb_delay_seconds": old_config.tmdb_delay_seconds,
                    "overwrite_existing_nfo": old_config.overwrite_existing_nfo,
                    "schedule_cron": old_cron,
                    "scheduler_enabled": old_config.scheduler_enabled,
                    "language": old_config.language,
                    "subtitle_enabled": old_config.subtitle_enabled,
                    "opensubtitles_api_key": old_config.opensubtitles_api_key,
                    "subtitle_languages": old_config.subtitle_languages,
                    "browse_root": old_config.browse_root,
                    "proxy": old_config.proxy,
                }
                try:
                    save_config(rollback, request.app.state.config_path)
                except Exception:
                    logger.exception("Rollback config failed")
                try:
                    request.app.state.scheduler.reschedule(old_cron)
                except Exception:
                    logger.exception("Rollback scheduler failed")
                # Close candidate scrapers
                await new_tmdb.aclose()
                if new_douban is not None:
                    await new_douban.aclose()
                return _redirect("/settings", err="设置保存失败，已回滚")

            # Update app state
            request.app.state.config = new_config
            request.app.state.tmdb = new_tmdb
            request.app.state.douban = new_douban

            # Close old scrapers (best-effort)
            try:
                await old_tmdb.aclose()
            except Exception:  # noqa: BLE001 (rollback on any failure)
                logger.warning("Failed to close old TMDB scraper")
            if old_douban is not None:
                try:
                    await old_douban.aclose()
                except Exception:  # noqa: BLE001 (rollback on any failure)
                    logger.warning("Failed to close old Douban scraper")
            if old_sub is not None:
                try:
                    await old_sub.aclose()
                except Exception:  # noqa: BLE001 (rollback on any failure)
                    logger.warning("Failed to close old subtitle downloader")

        return _redirect("/settings", ok="设置已保存")

    # ------------------------------------------------------------------
    # POST /items/clear-missing
    # ------------------------------------------------------------------

    @app.post("/items/clear-missing")
    async def items_clear_missing(request: Request) -> Any:
        """Delete all items in 'missing' status."""
        runner: ScanRunner = request.app.state.runner
        if runner.is_running:
            return _redirect("/items", err="任务正在运行中，暂不能清理")

        sess = request.app.state.session_factory()
        try:
            result = sess.execute(
                select(MediaItem).where(MediaItem.status == "missing")
            ).scalars().all()
            count = len(result)
            for item in result:
                sess.delete(item)
            sess.commit()
            return _redirect("/items", ok=f"已清除 {count} 条 missing 记录")
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # GET /preview — poster grid browse page
    # ------------------------------------------------------------------

    @app.get("/preview")
    async def preview(request: Request, genre: str = "", media_type: str = "") -> Any:
        """Browse matched items as a poster grid, optionally filtered by genre and media type."""
        sess = request.app.state.session_factory()
        try:
            stmt = select(MediaItem).where(MediaItem.status == "matched")
            if media_type in ("movie", "tv"):
                stmt = stmt.where(MediaItem.media_type == media_type)
            stmt = stmt.order_by(MediaItem.id.desc())

            items = sess.execute(stmt).scalars().all()

            # Collect all genres for the filter bar
            all_genres: set[str] = set()
            filtered_items: list[MediaItem] = []
            for item in items:
                item_genres = [g.strip() for g in (item.genres or "").split(",") if g.strip()]
                all_genres.update(item_genres)
                if genre and genre not in item_genres:
                    continue
                filtered_items.append(item)

            # Group by genre
            genre_groups: dict[str, list[MediaItem]] = {}
            for item in filtered_items:
                item_genres = [g.strip() for g in (item.genres or "").split(",") if g.strip()]
                for g in item_genres:
                    if genre and g != genre:
                        continue
                    genre_groups.setdefault(g, []).append(item)

            # If a specific genre is selected, only show that group
            if genre:
                genre_groups = {genre: genre_groups.get(genre, [])}

            sorted_genres = sorted(all_genres)

            return _render(
                "preview.html",
                {
                    "request": request,
                    "genre_groups": genre_groups,
                    "all_genres": sorted_genres,
                    "current_genre": genre,
                    "current_media_type": media_type,
                    "total_items": len(filtered_items),
                },
            )
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # GET /play/{item_id} — HTML5 video player page
    # ------------------------------------------------------------------

    @app.get("/play/{item_id}")
    async def play_page(request: Request, item_id: int) -> Any:
        """Render an HTML5 video player for a media item."""
        sess = request.app.state.session_factory()
        try:
            item = sess.get(MediaItem, item_id)
            if item is None:
                return HTMLResponse("<h2>条目不存在</h2>", status_code=404)
            if item.status != "matched":
                return HTMLResponse("<h2>该条目尚未刮削完成</h2>", status_code=400)

            title = item.matched_title or item.parsed_title or "Unknown"

            # Quick probe to get file info for the play page
            lib = sess.get(Library, item.library_id)
            file_info: dict[str, object] = {}
            if lib is not None:
                conn = _library_connection(lib, request.app.state.enc_key)
                try:
                    from app.scanner import _find_video_file_async, _relative_folder
                    rel = _relative_folder(item.folder_path, lib.path)
                    if Path(rel).suffix.lower() in VIDEO_EXTENSIONS:
                        video_rel = rel
                    else:
                        video_rel = await _find_video_file_async(conn, rel)
                        if video_rel is None:
                            video_rel = await _find_video_deep(conn, rel)
                    if video_rel is not None:
                        file_info = {
                            "found": True,
                            "ext": Path(video_rel).suffix.lower(),
                            "size": await conn.file_size(video_rel),
                            "filename": Path(video_rel).name,
                        }
                except Exception:
                    pass
                finally:
                    await conn.aclose()

            return HTMLResponse(_play_page_html(item_id, title, file_info))
        finally:
            sess.close()

    # ------------------------------------------------------------------
    # GET /api/stream/{item_id} — video streaming with Range support
    # ------------------------------------------------------------------

    @app.get("/api/stream/{item_id}")
    async def stream_video(request: Request, item_id: int) -> Any:
        """Stream a video file with HTTP Range support (for HTML5 <video>)."""
        from starlette.responses import StreamingResponse

        sess = request.app.state.session_factory()
        try:
            item = sess.get(MediaItem, item_id)
            if item is None:
                return JSONResponse({"error": "item not found"}, status_code=404)

            lib = sess.get(Library, item.library_id)
            if lib is None:
                return JSONResponse({"error": "library not found"}, status_code=404)

            conn = _library_connection(lib, request.app.state.enc_key)
        finally:
            sess.close()

        try:
            # Find the video file
            from app.scanner import _find_video_file_async, _relative_folder
            rel = _relative_folder(item.folder_path, lib.path)

            # First check: is the item itself a loose video file?
            if Path(rel).suffix.lower() in VIDEO_EXTENSIONS:
                video_rel = rel
            else:
                video_rel = await _find_video_file_async(conn, rel)
                # Fallback: search deeper (some folder structures nest video
                # files more than 2 levels deep, e.g. BDMV/STREAM/xxx.m2ts)
                if video_rel is None:
                    video_rel = await _find_video_deep(conn, rel)

            if video_rel is None:
                logger.warning(
                    "Stream: no video file found for item %s (rel=%s)",
                    item_id, rel,
                )
                await conn.aclose()
                return JSONResponse(
                    {"error": f"未找到可播放的视频文件: {rel}"},
                    status_code=404,
                )

            # Determine file size and MIME type
            file_size = await conn.file_size(video_rel)
            ext = Path(video_rel).suffix.lower()
            content_type = _video_mime(ext)

            # Parse Range header
            range_header = request.headers.get("range")
            if range_header:
                # Support "bytes=start-end" format
                import re
                m = re.match(r"bytes=(\d+)-(\d*)", range_header)
                if m:
                    start = int(m.group(1))
                    end_str = m.group(2)
                    if end_str:
                        end = min(int(end_str), file_size - 1)
                    else:
                        end = file_size - 1
                    chunk_size = end - start + 1

                    data = await conn.read_range(video_rel, start, chunk_size)
                    await conn.aclose()

                    headers: dict[str, str] = {
                        "Content-Range": f"bytes {start}-{end}/{file_size}",
                        "Accept-Ranges": "bytes",
                        "Content-Length": str(len(data)),
                        "Content-Type": content_type,
                    }
                    return StreamingResponse(
                        _single_chunk(data),
                        status_code=206,
                        headers=headers,
                    )

            # No range — stream full file in chunks
            CHUNK = 1024 * 1024  # 1 MiB
            async def _stream_full() -> Any:
                try:
                    pos = 0
                    while pos < file_size:
                        size = min(CHUNK, file_size - pos)
                        chunk = await conn.read_range(video_rel, pos, size)
                        if not chunk:
                            break
                        yield chunk
                        pos += len(chunk)
                finally:
                    await conn.aclose()

            return StreamingResponse(
                _stream_full(),
                status_code=200,
                headers={
                    "Content-Length": str(file_size),
                    "Content-Type": content_type,
                    "Accept-Ranges": "bytes",
                },
                media_type=content_type,
            )
        except Exception:
            await conn.aclose()
            raise

    # ------------------------------------------------------------------
    # GET /healthz
    # ------------------------------------------------------------------

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------
    # GET /api/browse — folder browser for library path selection
    # ------------------------------------------------------------------

    @app.get("/api/browse")
    async def api_browse(
        request: Request,
        path: str = "/",
        connection_type: str = "local",
        host: str = "",
        port: str = "",
        username: str = "",
        password: str = "",
        connection_id: str = "",
    ) -> Any:
        """Return a JSON list of subdirectories at *path*.

        For local connections, browsing is clamped under the configured
        ``browse_root`` so the modal only ever shows directories the scanner
        can actually reach (i.e. volumes mounted into the container).  When
        the container runs on Linux, each child directory is annotated with
        the host source from ``/proc/mounts`` so the admin can tell which
        mount corresponds to which host path.

        For remote connections, a temporary connection is created to browse.
        """
        from app.connection import ConnectionConfig, create_connection

        # Reuse an existing library's connection: resolve its (decrypted)
        # credentials server-side so the password never reaches the browser.
        if connection_id:
            sess = request.app.state.session_factory()
            try:
                try:
                    src = sess.get(Library, int(connection_id))
                except (TypeError, ValueError):
                    src = None
            finally:
                sess.close()
            if (
                src is None
                or src.connection_type == "local"
                or not src.connection_config_encrypted
            ):
                return JSONResponse(
                    {"error": "所选连接不可用（可能已被删除）", "items": []},
                    status_code=400,
                )
            try:
                cfg = decrypt_dict(src.connection_config_encrypted, request.app.state.enc_key)
            except Exception:  # noqa: BLE001
                return JSONResponse(
                    {"error": "连接凭据解密失败", "items": []}, status_code=500
                )
            connection_type = src.connection_type
            host = str(cfg.get("host", ""))
            port = str(cfg.get("port") or "")
            username = str(cfg.get("username", ""))
            password = str(cfg.get("password") or "")

        try:
            if connection_type == "local":
                config: AppConfig = request.app.state.config
                browse_root = Path(config.browse_root or "/")

                # Normalise & clamp under browse_root
                requested = Path(path)
                if not requested.is_absolute():
                    requested = browse_root / requested
                try:
                    requested = requested.resolve(strict=False)
                    browse_root_resolved = browse_root.resolve(strict=False)
                except (OSError, RuntimeError):
                    return JSONResponse(
                        {"error": f"路径解析失败: {path}", "items": []},
                        status_code=400,
                    )

                def _is_under(child: Path, parent: Path) -> bool:
                    try:
                        child.relative_to(parent)
                        return True
                    except ValueError:
                        return False

                if not _is_under(requested, browse_root_resolved) and requested != browse_root_resolved:
                    requested = browse_root_resolved

                if not requested.exists():
                    return JSONResponse(
                        {"error": f"路径不存在: {requested}", "items": []},
                        status_code=404,
                    )
                if not requested.is_dir():
                    return JSONResponse(
                        {"error": f"不是目录: {requested}", "items": []},
                        status_code=400,
                    )

                # Read /proc/mounts (Linux containers only) to annotate mount sources.
                # Normalise to forward slashes so the lookup works on Windows dev too.
                mount_map = _read_proc_mounts()
                mount_map_norm = {k.replace("\\", "/"): v for k, v in mount_map.items()}

                items = []
                for child in requested.iterdir():
                    if not child.is_dir():
                        continue
                    entry: dict[str, object] = {
                        "name": child.name,
                        "path": str(child),
                    }
                    src = mount_map_norm.get(str(child).replace("\\", "/"))
                    if src:
                        entry["mount_source"] = src
                    items.append(entry)
                items.sort(key=lambda d: str(d["name"]).lower())

                parent_path: str | None = None
                if requested != browse_root_resolved:
                    parent_path = str(requested.parent)

                return {
                    "items": items,
                    "parent": parent_path,
                    "current": str(requested),
                    "browse_root": str(browse_root_resolved),
                }

            # Remote: create a temp connection
            if not host.strip():
                return JSONResponse({"error": "请输入主机地址", "items": []}, status_code=400)
            try:
                browse_port = int(port) if port.strip() else 0
            except ValueError:
                return JSONResponse({"error": "端口必须是数字", "items": []}, status_code=400)
            cfg = ConnectionConfig(
                type=connection_type,
                host=host.strip(),
                port=browse_port,
                username=username.strip(),
                password=password,
            )
            conn = create_connection(cfg, "")
            try:
                entries = await conn.list_dir(path)
                dirs_only: list[dict[str, str]] = []
                for name in entries:
                    full = str(PurePosixPath(path) / name)
                    try:
                        if await conn.is_dir(full):
                            dirs_only.append({"name": name, "path": full})
                    except Exception as exc:  # noqa: BLE001 (browse is best-effort)
                        logger.debug("Browse is_dir failed for %s: %s", full, exc)
                dirs_only.sort(key=lambda d: d["name"].lower())
                # Allow navigating back up to root "/"; only hide "parent"
                # when we are already at the root (matches the local branch).
                parent = None if path == "/" else str(PurePosixPath(path).parent)
                return {"items": dirs_only, "parent": parent, "current": path}
            finally:
                await conn.aclose()

        except Exception as exc:  # noqa: BLE001 (browse is best-effort, return error JSON)
            return JSONResponse({"error": str(exc), "items": []}, status_code=500)

    return app



# ---------------------------------------------------------------------------
# Seed import (one-time)
# ---------------------------------------------------------------------------


def _run_seed_import(session_factory: Any, config: AppConfig) -> None:
    """Import libraries from config YAML seed on first startup only."""
    with session_factory.begin() as sess:
        marker = sess.get(AppMeta, "libraries_seed_imported")
        if marker is not None:
            return  # Already imported

        # Import valid seeds
        for seed in config.libraries_seed:
            if not seed.path.startswith("/"):
                logger.warning("Seed library path is not absolute, skipping: %s", seed.path)
                continue
            norm = normalize_path(seed.path)
            existing = sess.execute(
                select(Library).where(Library.path == norm)
            ).scalar_one_or_none()
            if existing is not None:
                logger.warning("Seed library path already exists, skipping: %s", norm)
                continue
            lib = Library(name=seed.name, path=norm, media_type=seed.type)
            sess.add(lib)

        # Persist marker
        meta = AppMeta(key="libraries_seed_imported", value="1")
        sess.add(meta)


# ---------------------------------------------------------------------------
# Template filters
# ---------------------------------------------------------------------------


def _filter_localtime(dt: datetime | None) -> str:
    """UTC datetime → local time string for display."""
    if dt is None:
        return ""
    local = dt.replace(tzinfo=UTC).astimezone().replace(tzinfo=None)
    return local.strftime("%Y-%m-%d %H:%M:%S")


def _filter_mask_key(key: str) -> str:
    """Mask an API key for display."""
    if not key:
        return ""
    if len(key) <= 4:
        return "*" * len(key)
    return "*" * (len(key) - 4) + key[-4:]


def _format_time(dt: datetime | None) -> str:
    """Format next_run_time for the dashboard."""
    if dt is None:
        return "未启用"
    return _filter_localtime(dt)


def _play_page_html(item_id: int, title: str, file_info: dict[str, object] | None = None) -> str:
    """Return an HTML5 video player page for *item_id* with codec guidance."""
    stream_url = f"/api/stream/{item_id}"

    # Determine browser compatibility
    ext = str(file_info.get("ext", "")) if file_info else ""
    fname = str(file_info.get("filename", "")) if file_info else ""
    fsize_mb = ""
    if file_info and file_info.get("size"):
        try:
            fsize_mb = f"{(int(file_info['size']) / 1048576):.1f} MB"
        except (ValueError, TypeError):
            pass

    browser_ok = ext in (".mp4", ".webm", ".mov")
    format_label = ext.upper().lstrip(".") if ext else "?"
    format_note = ""
    if ext == ".mkv":
        format_note = "MKV 容器通常含 HEVC/DTS 编码，浏览器无法解码"
    elif ext == ".m2ts":
        format_note = "M2TS 蓝光原盘格式，浏览器不支持"
    elif ext == ".ts":
        format_note = "TS 流格式，浏览器兼容性差"
    elif ext == ".avi":
        format_note = "AVI 容器较旧，浏览器可能无法播放"

    file_info_html = ""
    if file_info and file_info.get("found"):
        file_info_html = f"""
<div style="background:#1a1a2e;border-radius:8px;padding:12px 16px;margin-bottom:16px;font-size:13px;line-height:1.8;">
  <div>📁 <b>{fname}</b></div>
  <div style="color:#888;">格式: {format_label} | 大小: {fsize_mb}</div>
  <div style="color:#e67e22;margin-top:4px;">{format_note}</div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title} — 在线播放</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ background:#0a0a0a; color:#ccc; font-family:-apple-system,sans-serif; display:flex; flex-direction:column; height:100vh; }}
.header {{ padding:10px 16px; background:#1a1a1a; display:flex; align-items:center; gap:12px; flex-shrink:0; }}
.header a {{ color:#3498db; text-decoration:none; font-size:14px; }}
.header h2 {{ font-size:16px; flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }}
.player-wrap {{ flex:1; display:flex; flex-direction:column; align-items:center; justify-content:center; position:relative; min-height:0; padding:20px; }}
video {{ max-width:100%; max-height:60vh; background:#000; border-radius:4px; }}
.loading {{ display:none; position:absolute; top:50%; left:50%; transform:translate(-50%,-50%); text-align:center; z-index:10; }}
.spinner {{ width:40px; height:40px; border:3px solid #333; border-top-color:#3498db; border-radius:50%; animation:spin 0.8s linear infinite; margin:0 auto 12px; }}
@keyframes spin {{ to {{ transform:rotate(360deg); }} }}
.fallback {{ padding:20px; text-align:center; max-width:500px; }}
.fallback code {{ display:block; margin:12px auto; padding:10px; background:#1a1a1a; border-radius:6px; word-break:break-all; font-size:13px; }}
.fallback button {{ margin:6px; padding:10px 20px; border:none; border-radius:6px; cursor:pointer; font-size:14px; }}
.btn-copy {{ background:#2c3e50; color:#fff; }}
.btn-pot {{ background:#e67e22; color:#fff; }}
.btn-back {{ background:#555; color:#fff; }}
</style>
</head>
<body>
<div class="header">
  <a href="/preview">← 返回影视库</a>
  <h2>{title}</h2>
  <span id="status" style="font-size:12px;color:#888;"></span>
</div>
<div class="player-wrap" id="playerWrap">
  {file_info_html}
  <div id="loading" class="loading" style="display:flex;flex-direction:column;align-items:center;">
    <div class="spinner"></div><div>正在检测视频流…</div>
  </div>
  <video id="player" controls style="display:none;"
         onloadedmetadata="onLoaded()"
         onerror="onError()"
         onwaiting="showLoading()"
         onplaying="hideLoading()"
         onabort="onError()"
         onstalled="onError()">
    <source src="{stream_url}" onerror="onSourceError()">
  </video>
  <div id="fallback" class="fallback">
    <h3 style="color:#e74c3c;margin-bottom:8px;">⚠ 浏览器无法播放此视频</h3>
    <p style="color:#f99;font-size:13px;margin-bottom:4px;" id="fallbackReason"></p>
    <p style="font-size:13px;color:#e67e22;margin-bottom:12px;">{format_note}</p>
    <p style="margin-bottom:4px;font-weight:600;">请使用 PotPlayer 或 VLC 播放：</p>
    <code id="streamUrl">{stream_url}</code>
    <button class="btn-copy" onclick="copyStreamUrl()">📋 复制流地址</button>
    <button class="btn-pot" onclick="potPlayer()">🎬 一键复制到 PotPlayer</button>
    <a href="/preview"><button class="btn-back">← 返回影视库</button></a>
    <p style="margin-top:16px;font-size:12px;color:#888;line-height:1.6;">
      <b>PotPlayer 步骤：</b>打开 PotPlayer → 按 Ctrl+U → 粘贴地址 → 确定<br>
      <b>VLC 步骤：</b>媒体 → 打开网络串流 → 粘贴地址 → 播放<br>
      <b>为什么浏览器不能播？</b>REMUX/蓝光文件使用 HEVC 10bit HDR + DTS-HD/TrueHD 音轨，这些编码浏览器不支持硬解。PotPlayer/VLC 内置了完整的解码器。
    </p>
  </div>
</div>
<script>
var streamUrl = '{stream_url}';
var fullUrl = window.location.origin + streamUrl;
var _probeDone = false;
var _loadTimer = null;
var _browserOk = {'true' if browser_ok else 'false'};

document.getElementById('streamUrl').textContent = fullUrl;

// Show fallback immediately for known-unsupported formats
if (!_browserOk) {{
  document.getElementById('loading').style.display = 'none';
  document.getElementById('fallback').style.display = 'block';
  document.getElementById('fallbackReason').textContent = '{format_label} 格式 — 推荐使用外部播放器';
  document.getElementById('status').textContent = '需外部播放器';
}} else {{
  // Probe: first byte range request to verify the stream
  fetch(fullUrl, {{ headers: {{ 'Range': 'bytes=0-0' }} }}).then(function(r) {{
    if (r.status === 206) {{ startPlayer(); return; }}
    r.json().then(function(j) {{ showFallback(j.error || 'HTTP ' + r.status); }})
     .catch(function() {{ showFallback('服务器返回 HTTP ' + r.status); }});
  }}).catch(function(e) {{ showFallback('无法连接: ' + e.message); }});
}}

function startPlayer() {{
  if (_probeDone) return;
  _probeDone = true;
  document.getElementById('loading').style.display = 'flex';
  document.querySelector('#loading div:last-child').textContent = '正在加载视频…';
  var v = document.getElementById('player');
  v.style.display = 'block';
  v.load();
  _loadTimer = setTimeout(function() {{
    if (v.readyState < 2) showFallback('加载超时 — 编码可能不兼容，请用 PotPlayer');
  }}, 15000);
}}

function showFallback(reason) {{
  if (_loadTimer) clearTimeout(_loadTimer);
  document.getElementById('loading').style.display = 'none';
  var v = document.getElementById('player');
  v.style.display = 'none';
  v.pause(); v.removeAttribute('src');
  document.getElementById('fallback').style.display = 'block';
  document.getElementById('fallbackReason').textContent = reason || '';
  document.getElementById('status').textContent = '播放失败';
}}

function showLoading() {{ document.getElementById('loading').style.display = 'flex'; }}
function hideLoading() {{ document.getElementById('loading').style.display = 'none'; }}

function onLoaded() {{
  hideLoading();
  document.getElementById('player').style.display = 'block';
  document.getElementById('status').textContent = '正在播放';
  if (_loadTimer) clearTimeout(_loadTimer);
}}

function onError() {{
  var v = document.getElementById('player');
  var msg = v.error ? v.error.message : '未知错误';
  showFallback('解码失败: ' + msg + ' — 请用 PotPlayer 播放');
}}

function onSourceError() {{ showFallback('视频源加载失败 — 格式不支持'); }}

function copyStreamUrl() {{
  if (navigator.clipboard) {{
    navigator.clipboard.writeText(fullUrl).then(function() {{
      alert('流地址已复制！\\n\\n打开 PotPlayer (Ctrl+U) 或 VLC (Ctrl+N) 粘贴即可播放。');
    }});
  }} else {{ prompt('请复制：', fullUrl); }}
}}

function potPlayer() {{
  if (navigator.clipboard) {{
    navigator.clipboard.writeText(fullUrl).then(function() {{
      alert('流地址已复制！\\n\\n请打开 PotPlayer → 按 Ctrl+U → 粘贴 → 确定');
    }});
  }}
}}
</script>
</body>
</html>"""


async def _find_video_deep(conn: Any, rel: str, depth: int = 4) -> str | None:
    """Recursively search for a video file under *rel* up to *depth* levels.

    Used as a fallback when the standard 2-level search doesn't find anything
    (e.g. deeply nested BDMV structures or non-standard layouts).
    """
    if depth <= 0:
        return None
    try:
        entries = await conn.list_dir(rel)
    except OSError:
        return None
    # Check files first (prefer shallowest)
    for name in sorted(entries):
        child = str(PurePosixPath(rel) / name)
        try:
            if await conn.is_file(child) and Path(name).suffix.lower() in VIDEO_EXTENSIONS:
                return child
        except OSError:
            continue
    # Then recurse into subdirs
    for name in sorted(entries):
        child = str(PurePosixPath(rel) / name)
        try:
            if await conn.is_dir(child):
                found = await _find_video_deep(conn, child, depth - 1)
                if found is not None:
                    return found
        except OSError:
            continue
    return None


def _video_mime(ext: str) -> str:
    """Return the MIME type for a video file extension."""
    return {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".avi": "video/x-msvideo",
        ".mov": "video/quicktime",
        ".ts": "video/mp2t",
        ".m2ts": "video/mp2t",
    }.get(ext, "application/octet-stream")


async def _single_chunk(data: bytes) -> Any:
    """Async generator that yields a single chunk (for range responses)."""
    yield data


def _read_proc_mounts() -> dict[str, str]:
    """Return a mapping of mountpoint -> source device from ``/proc/mounts``.

    Used to annotate local browse results so the admin can see which host
    path a container directory was mounted from.  Returns an empty dict on
    non-Linux platforms or when ``/proc/mounts`` is unavailable.
    """
    proc = Path("/proc/mounts")
    if not proc.is_file():
        return {}
    try:
        lines = proc.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {}
    result: dict[str, str] = {}
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        source, mountpoint, _fstype = parts[0], parts[1], parts[2]
        # Skip pseudo filesystems' own root; keep real bind mounts.
        if mountpoint in ("/", "/proc", "/sys", "/dev", "/etc/hosts", "/etc/resolv.conf"):
            continue
        # Prefer the host source; fall back to filesystem type if no device.
        if source.startswith(("/", "\\\\")):
            result[mountpoint] = source
        elif source.lower() not in ("overlay", "tmpfs", "proc", "sysfs", "devtmpfs", "cgroup", "cgroup2"):
            result[mountpoint] = f"[{source}]"
    return result


# Module-level app instance (for ``uvicorn app.main:app`` / production).
# Tests import ``create_app`` directly and pass their own ``data_dir``.
app = create_app()
