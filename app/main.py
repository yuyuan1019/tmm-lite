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

from app import get_data_dir
from app.config import AppConfig, load_config, save_config, validate_cron
from app.crypto import load_or_create_key
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
from app.scanner import ScanRunner, _ignored_paths, _set_ignored_paths, normalize_path
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

            return _render(
                "libraries.html",
                {
                    "request": request,
                    "libraries": lib_data,
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
    ) -> Any:
        runner: ScanRunner = request.app.state.runner
        if runner.is_running:
            return _redirect("/libraries", err="任务正在运行中，暂不能修改媒体库")

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

            # Encrypt connection config if not local
            enc_config: str | None = None
            if connection_type != "local":
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
    # POST /libraries/{id}/delete
    # ------------------------------------------------------------------

    @app.post("/libraries/{lib_id}/delete")
    async def libraries_delete(request: Request, lib_id: int) -> Any:
        runner: ScanRunner = request.app.state.runner
        if runner.is_running:
            return _redirect("/libraries", err="任务正在运行中，暂不能修改媒体库")

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

            return _render(
                "items.html",
                {
                    "request": request,
                    "items": items,
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
        try:
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
                parent_path = str(PurePosixPath(path).parent)
                parent = parent_path if parent_path and parent_path != "/" else None
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
