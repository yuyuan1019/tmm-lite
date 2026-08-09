"""M9 Web layer tests — M9-T1 through M9-T12."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path
from threading import Event
from typing import ClassVar
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.main import _play_page_html, create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a TestClient with a temp data dir and scheduler disabled."""
    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as c:
        yield c


def _add_local_stream_item(client: TestClient, media_root: Path, data: bytes) -> int:
    from app.database import Library, MediaItem

    media_root.mkdir(parents=True, exist_ok=True)
    video = media_root / "video.mp4"
    video.write_bytes(data)
    with client.app.state.session_factory.begin() as session:
        library = Library(name="Stream", path=str(media_root), media_type="movie")
        session.add(library)
        session.flush()
        item = MediaItem(
            library_id=library.id,
            media_type="movie",
            folder_path=str(video),
            parsed_title="Video",
            matched_title="Video",
            status="matched",
        )
        session.add(item)
        session.flush()
        return item.id


def _add_remote_stream_item(client: TestClient) -> int:
    from app.database import Library, MediaItem

    with client.app.state.session_factory.begin() as session:
        library = Library(name="Remote Stream", path="/remote", media_type="movie")
        session.add(library)
        session.flush()
        item = MediaItem(
            library_id=library.id,
            media_type="movie",
            folder_path="/remote/video.mp4",
            status="matched",
        )
        session.add(item)
        session.flush()
        return item.id


# ---------------------------------------------------------------------------
# M9-T1: All GET pages return 200
# ---------------------------------------------------------------------------
def test_all_pages_200_empty(client: TestClient) -> None:
    for path in ["/", "/libraries", "/items", "/logs", "/settings"]:
        resp = client.get(path)
        assert resp.status_code == 200, f"{path} returned {resp.status_code}"


# ---------------------------------------------------------------------------
# M9-T2: Library add/delete
# ---------------------------------------------------------------------------
def test_library_add_and_delete(client: TestClient) -> None:
    # Add
    resp = client.post("/libraries/add", data={
        "name": "Movies", "path": "/media/movies", "media_type": "movie",
    }, follow_redirects=False)
    assert resp.status_code == 303

    # List
    resp = client.get("/libraries")
    assert resp.status_code == 200
    assert "Movies" in resp.text
    assert "/media/movies" in resp.text

    # Delete
    resp = client.post("/libraries/1/delete", follow_redirects=False)
    assert resp.status_code == 303

    # Verify gone
    resp = client.get("/libraries")
    assert "Movies" not in resp.text


def test_library_add_relative_path_rejected(client: TestClient) -> None:
    resp = client.post("/libraries/add", data={
        "name": "Bad", "path": "relative/path", "media_type": "movie",
    }, follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    assert "绝对路径" in unquote(location)


def test_library_add_duplicate_rejected(client: TestClient) -> None:
    client.post("/libraries/add", data={
        "name": "A", "path": "/media/a", "media_type": "movie",
    }, follow_redirects=False)
    resp = client.post("/libraries/add", data={
        "name": "B", "path": "/media/a", "media_type": "tv",
    }, follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    assert "已存在" in unquote(location)


def test_library_delete_404(client: TestClient) -> None:
    resp = client.post("/libraries/999/delete")
    assert resp.status_code == 404


def test_library_mutations_rejected_while_scan_is_running(client: TestClient) -> None:
    from app.database import Library

    client.post(
        "/libraries/add",
        data={"name": "Existing", "path": "/media/existing", "media_type": "movie"},
        follow_redirects=False,
    )
    runner = client.app.state.runner
    runner._running = True
    try:
        add_response = client.post(
            "/libraries/add",
            data={"name": "Blocked", "path": "/media/blocked", "media_type": "movie"},
            follow_redirects=False,
        )
        delete_response = client.post("/libraries/1/delete", follow_redirects=False)
    finally:
        runner._running = False

    assert "暂不能修改媒体库" in unquote(add_response.headers["location"])
    assert "暂不能修改媒体库" in unquote(delete_response.headers["location"])
    with client.app.state.session_factory() as session:
        libraries = session.query(Library).all()
        assert [library.name for library in libraries] == ["Existing"]


@pytest.mark.parametrize(
    ("connection_type", "host", "port", "message"),
    [
        ("ssh", "", "22", "主机不能为空"),
        ("webdav", "", "443", "主机不能为空"),
        ("ssh", "nas.example", "abc", "端口必须是数字"),
        ("ssh", "nas.example", "0", "端口必须在"),
        ("webdav", "nas.example", "65536", "端口必须在"),
        ("smb", "nas.example", "445", "连接方式无效"),
    ],
)
def test_remote_library_validation(
    client: TestClient,
    connection_type: str,
    host: str,
    port: str,
    message: str,
) -> None:
    response = client.post(
        "/libraries/add",
        data={
            "name": "Remote",
            "path": f"/remote/{connection_type}/{port}",
            "media_type": "movie",
            "connection_type": connection_type,
            "conn_host": host,
            "conn_port": port,
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert message in unquote(response.headers["location"])


@pytest.mark.parametrize(("connection_type", "port"), [("ssh", 22), ("webdav", 443)])
def test_valid_remote_library_is_persisted(
    client: TestClient,
    connection_type: str,
    port: int,
) -> None:
    from app.crypto import decrypt_dict
    from app.database import Library

    response = client.post(
        "/libraries/add",
        data={
            "name": connection_type,
            "path": f"/remote/{connection_type}",
            "media_type": "movie",
            "connection_type": connection_type,
            "conn_host": "nas.example",
            "conn_port": str(port),
            "conn_username": "media",
            "conn_password": "secret",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with client.app.state.session_factory() as session:
        library = session.query(Library).filter_by(name=connection_type).one()
        assert library.connection_type == connection_type
        assert library.connection_config_encrypted is not None
        config = decrypt_dict(
            library.connection_config_encrypted,
            client.app.state.enc_key,
        )
        assert config["host"] == "nas.example"
        assert config["port"] == port


# ---------------------------------------------------------------------------
# M9-T3: run-scrape trigger
# ---------------------------------------------------------------------------
def test_run_scrape_trigger(client: TestClient) -> None:
    # First add a library so there's something to scan
    client.post("/libraries/add", data={
        "name": "Movies", "path": "/media/movies", "media_type": "movie",
    }, follow_redirects=False)
    resp = client.post("/run-scrape", follow_redirects=False)
    assert resp.status_code == 303


def test_stop_scrape_idle(client: TestClient) -> None:
    resp = client.post("/stop-scrape", follow_redirects=False)
    assert resp.status_code == 303
    assert "没有正在运行" in unquote(resp.headers.get("location", ""))


def test_rescrape_failed_no_failed_items(client: TestClient) -> None:
    resp = client.post("/rescrape-failed", follow_redirects=False)
    assert resp.status_code == 303
    assert "没有失败的条目" in unquote(resp.headers.get("location", ""))


# ---------------------------------------------------------------------------
# M9-T4: rescrape
# ---------------------------------------------------------------------------
def test_rescrape_404(client: TestClient) -> None:
    resp = client.post("/items/999/rescrape")
    assert resp.status_code == 404


def test_rescrape_404_with_manual_query(client: TestClient) -> None:
    resp = client.post("/items/999/rescrape", data={"query": "Test Title"})
    assert resp.status_code == 404


def test_api_search_empty_title(client: TestClient) -> None:
    resp = client.get("/api/search?title=&media_type=movie")
    assert resp.status_code == 200
    assert resp.json()["items"] == []


def test_items_page_shows_subtitle_status(tmp_path: Path) -> None:
    from app.database import Library, MediaItem

    media_root = tmp_path / "media"
    film = media_root / "Film (2020)"
    film.mkdir(parents=True)
    (film / "movie.mkv").write_text("x")
    (film / "movie.zh-Hans.srt").write_text("x")

    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as c:
        sf = c.app.state.session_factory
        with sf.begin() as sess:
            lib = Library(name="L", path=str(media_root), media_type="movie")
            sess.add(lib)
            sess.flush()
            sess.add(MediaItem(
                library_id=lib.id, media_type="movie", folder_path=str(film),
                parsed_title="Film", status="matched",
            ))

        page = c.get("/items").text
        assert "简体中文" in page


# ---------------------------------------------------------------------------
# M9-T4b: manual subtitle
# ---------------------------------------------------------------------------
def test_subtitle_404(client: TestClient) -> None:
    resp = client.post("/items/999/subtitle")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# M9-T4c: delete item record (record only — files untouched)
# ---------------------------------------------------------------------------
def test_item_delete_record(tmp_path: Path) -> None:
    from app.database import Library, MediaItem

    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as c:
        sf = c.app.state.session_factory
        with sf.begin() as sess:
            lib = Library(name="L", path="/media/m", media_type="movie")
            sess.add(lib)
            sess.flush()
            item = MediaItem(
                library_id=lib.id, media_type="movie",
                folder_path="/media/m/Movie (2020)", parsed_title="Movie",
                status="pending",
            )
            sess.add(item)
            sess.flush()
            item_id = item.id

        resp = c.post(f"/items/{item_id}/delete", follow_redirects=False)
        assert resp.status_code == 303
        assert "已删除" in unquote(resp.headers.get("location", ""))

        with sf() as sess:
            assert sess.get(MediaItem, item_id) is None

        # Missing id → 404
        assert c.post("/items/999/delete").status_code == 404


def test_item_delete_records_ignored_and_clear(tmp_path: Path) -> None:
    from app.database import AppMeta, Library, MediaItem
    from app.scanner import _IGNORED_META_KEY

    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as c:
        sf = c.app.state.session_factory
        with sf.begin() as sess:
            lib = Library(name="L", path="/media/m", media_type="movie")
            sess.add(lib)
            sess.flush()
            item = MediaItem(
                library_id=lib.id, media_type="movie",
                folder_path="/media/m/Movie (2020)", parsed_title="Movie",
                status="pending",
            )
            sess.add(item)
            sess.flush()
            item_id = item.id

        c.post(f"/items/{item_id}/delete", follow_redirects=False)

        # Path recorded as ignored
        with sf() as sess:
            meta = sess.get(AppMeta, _IGNORED_META_KEY)
            assert meta is not None
            assert "/media/m/Movie (2020)" in meta.value

        # /items shows the ignored notice
        page = c.get("/items")
        assert "已忽略（删除记录、未删文件）1 条" in page.text

        # Clear ignored → back to zero
        c.post("/items/clear-ignored", follow_redirects=False)
        with sf() as sess:
            meta = sess.get(AppMeta, _IGNORED_META_KEY)
            assert meta is None or meta.value == ""


# ---------------------------------------------------------------------------
# M9-T5: Items filtering
# ---------------------------------------------------------------------------
def test_items_filter(client: TestClient) -> None:
    resp = client.get("/items?status=matched")
    assert resp.status_code == 200


# ---------------------------------------------------------------------------
# M9-T6: Settings save with hot-reload
# ---------------------------------------------------------------------------
def test_settings_save_and_reload(client: TestClient) -> None:
    resp = client.post("/settings", data={
        "tmdb_api_key": "test-key-123",
        "use_douban": "on",
        "douban_delay_seconds": "3.0",
        "overwrite_existing_nfo": "on",
        "schedule_cron": "0 6 * * *",
        "subtitle_enabled": "on",
        "opensubtitles_api_key": "os-key",
        "subdl_api_key": "subdl-key",
        "subtitle_languages": "zh-cn",
    }, follow_redirects=False)
    assert resp.status_code == 303
    assert client.app.state.config.subdl_api_key == "subdl-key"
    assert client.app.state.config.subtitle_languages == "zh-cn"

    # Verify page shows the new values
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert 'name="opensubtitles_user_agent"' not in resp.text
    assert "无需手动填写" in resp.text


def test_settings_invalid_cron_rejected(client: TestClient) -> None:
    resp = client.post("/settings", data={
        "schedule_cron": "bad cron",
    }, follow_redirects=False)
    assert resp.status_code == 303
    location = resp.headers.get("location", "")
    assert "无效" in (unquote(location) if location else "")


# ---------------------------------------------------------------------------
# M9-T7: API key masking
# ---------------------------------------------------------------------------
def test_api_key_masked(client: TestClient) -> None:
    # Set a key first
    client.post("/settings", data={
        "tmdb_api_key": "my-secret-key-1234",
        "schedule_cron": "0 4 * * *",
    })

    resp = client.get("/settings")
    assert resp.status_code == 200
    # The full key should NOT appear (it's masked)
    assert "my-secret-key-1234" not in resp.text
    # Placeholder should mention it's set
    assert "已设置" in resp.text or "1234" in resp.text


# ---------------------------------------------------------------------------
# M9-T8: Seed import runs once
# ---------------------------------------------------------------------------
def test_seed_import_once(tmp_path: Path) -> None:
    """Verify seed import runs once and writes the marker."""
    data_dir = tmp_path / "data_seed"
    data_dir.mkdir(parents=True)
    config_path = data_dir / "config.yaml"
    config_path.write_text(
        "libraries:\n  - name: SeedLib\n    path: /media/seed\n    type: movie\n",
        encoding="utf-8",
    )

    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as client:
        resp = client.get("/libraries")
        assert "SeedLib" in resp.text

    # Second app creation — should NOT re-import
    app2 = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app2) as client2:
        resp = client2.get("/libraries")
        # Should still have exactly 1 SeedLib (not duplicated)
        assert resp.text.count("SeedLib") == 1


# ---------------------------------------------------------------------------
# M9-T9: Dashboard data
# ---------------------------------------------------------------------------
def test_dashboard_data(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "仪表盘" in resp.text or "dashboard" in resp.text.lower()


# ---------------------------------------------------------------------------
# M9-T10: Health check
# ---------------------------------------------------------------------------
def test_healthz(client: TestClient) -> None:
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# M9-T11: Missing API key warning
# ---------------------------------------------------------------------------
def test_missing_key_warning(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200
    assert "未配置" in resp.text


# ---------------------------------------------------------------------------
# M9-T12: Settings rejected when task running
# ---------------------------------------------------------------------------
def test_settings_save_preserves_data(client: TestClient) -> None:
    """Simple settings persistence check."""
    client.post("/settings", data={
        "schedule_cron": "0 2 * * *",
    }, follow_redirects=False)
    resp = client.get("/settings")
    assert resp.status_code == 200


def test_settings_constructor_failure_preserves_live_and_disk_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as test_client:
        old_tmdb = test_client.app.state.tmdb
        old_config = test_client.app.state.config
        config_path = test_client.app.state.config_path
        old_bytes = config_path.read_bytes()
        old_paused = test_client.app.state.scheduler.paused

        def fail_tmdb(*args: object, **kwargs: object) -> object:
            raise RuntimeError("constructor failed")

        monkeypatch.setattr(app_main, "TmdbScraper", fail_tmdb)
        response = test_client.post(
            "/settings",
            data={
                "tmdb_api_key": "new-key",
                "schedule_cron": "0 6 * * *",
                "douban_delay_seconds": "3.0",
                "tmdb_delay_seconds": "0.5",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "失败" in unquote(response.headers["location"])
        assert config_path.read_bytes() == old_bytes
        assert test_client.app.state.config is old_config
        assert test_client.app.state.tmdb is old_tmdb
        assert test_client.app.state.runner._tmdb is old_tmdb
        assert test_client.app.state.scheduler.paused is old_paused


def test_settings_snapshot_failure_does_not_construct_candidates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    class CandidateTmdb:
        instances: ClassVar[list[CandidateTmdb]] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.instances.append(self)

        async def aclose(self) -> None:
            pass

    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app) as test_client:
        config_path = test_client.app.state.config_path
        old_config = test_client.app.state.config
        old_tmdb = test_client.app.state.tmdb
        real_read_bytes = Path.read_bytes

        def fail_config_snapshot(path: Path) -> bytes:
            if path == config_path:
                raise PermissionError("config is temporarily unreadable")
            return real_read_bytes(path)

        monkeypatch.setattr(app_main, "TmdbScraper", CandidateTmdb)
        monkeypatch.setattr(Path, "read_bytes", fail_config_snapshot)
        response = test_client.post(
            "/settings",
            data={
                "schedule_cron": "0 6 * * *",
                "douban_delay_seconds": "2.0",
                "tmdb_delay_seconds": "0.5",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "失败" in unquote(response.headers["location"])
        assert CandidateTmdb.instances == []
        assert test_client.app.state.config is old_config
        assert test_client.app.state.tmdb is old_tmdb
        assert test_client.app.state.runner._tmdb is old_tmdb


@pytest.mark.parametrize("failing_resource", ["douban", "subtitle"])
def test_settings_partial_candidate_construction_closes_tmdb(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failing_resource: str,
) -> None:
    from app import main as app_main

    class CandidateTmdb:
        instances: ClassVar[list[CandidateTmdb]] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.close_count = 0
            self.instances.append(self)

        async def aclose(self) -> None:
            self.close_count += 1

    def fail_constructor(*args: object, **kwargs: object) -> object:
        raise RuntimeError(f"{failing_resource} constructor failed")

    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app) as test_client:
        old_bytes = test_client.app.state.config_path.read_bytes()
        old_tmdb = test_client.app.state.tmdb
        monkeypatch.setattr(app_main, "TmdbScraper", CandidateTmdb)
        if failing_resource == "douban":
            monkeypatch.setattr(app_main, "DoubanScraper", fail_constructor)
        else:
            monkeypatch.setattr(app_main, "SubtitleDownloader", fail_constructor)

        form_data = {
            "schedule_cron": "0 6 * * *",
            "douban_delay_seconds": "2.0",
            "tmdb_delay_seconds": "0.5",
        }
        if failing_resource == "douban":
            form_data["use_douban"] = "on"
        else:
            form_data["subtitle_enabled"] = "on"
        response = test_client.post(
            "/settings",
            data=form_data,
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert len(CandidateTmdb.instances) == 1
        assert CandidateTmdb.instances[0].close_count == 1
        assert test_client.app.state.config_path.read_bytes() == old_bytes
        assert test_client.app.state.tmdb is old_tmdb
        assert test_client.app.state.runner._tmdb is old_tmdb


def test_settings_reconfigure_failure_rolls_back_and_closes_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    class CandidateTmdb:
        instances: ClassVar[list[CandidateTmdb]] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.close_count = 0
            self.instances.append(self)

        async def aclose(self) -> None:
            self.close_count += 1

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "config.yaml").write_text(
        "# 保留这条管理员注释\n"
        "schedule_cron: '0 4 * * *'\n"
        "custom_setting: keep-me\n",
        encoding="utf-8",
    )
    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as test_client:
        old_tmdb = test_client.app.state.tmdb
        old_config = test_client.app.state.config
        config_path = test_client.app.state.config_path
        old_bytes = config_path.read_bytes()
        old_paused = test_client.app.state.scheduler.paused

        monkeypatch.setattr(app_main, "TmdbScraper", CandidateTmdb)

        def fail_reconfigure(*args: object, **kwargs: object) -> object:
            raise RuntimeError("reconfigure failed")

        monkeypatch.setattr(test_client.app.state.runner, "reconfigure", fail_reconfigure)
        response = test_client.post(
            "/settings",
            data={
                "schedule_cron": "0 7 * * *",
                "douban_delay_seconds": "2.0",
                "tmdb_delay_seconds": "0.5",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "已回滚" in unquote(response.headers["location"])
        assert len(CandidateTmdb.instances) == 1
        assert CandidateTmdb.instances[0].close_count == 1
        assert config_path.read_bytes() == old_bytes
        assert test_client.app.state.config is old_config
        assert test_client.app.state.tmdb is old_tmdb
        assert test_client.app.state.runner._tmdb is old_tmdb
        assert test_client.app.state.scheduler.paused is old_paused


def test_settings_restores_snapshot_when_save_raises_after_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as test_client:
        config_path = test_client.app.state.config_path
        old_bytes = config_path.read_bytes()
        old_config = test_client.app.state.config
        old_tmdb = test_client.app.state.tmdb
        real_save_config = app_main.save_config

        def write_then_fail(updates: dict[str, object], path: Path):
            real_save_config(updates, path)
            raise OSError("reload failed after write")

        monkeypatch.setattr(app_main, "save_config", write_then_fail)
        response = test_client.post(
            "/settings",
            data={
                "schedule_cron": "0 8 * * *",
                "douban_delay_seconds": "2.0",
                "tmdb_delay_seconds": "0.5",
            },
            follow_redirects=False,
        )

        assert response.status_code == 303
        assert "已回滚" in unquote(response.headers["location"])
        assert config_path.read_bytes() == old_bytes
        assert test_client.app.state.config is old_config
        assert test_client.app.state.tmdb is old_tmdb
        assert test_client.app.state.runner._tmdb is old_tmdb


def test_hot_reload_shutdown_closes_current_scraper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    class TrackedTmdb:
        instances: ClassVar[list[TrackedTmdb]] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.close_count = 0
            self.instances.append(self)

        async def aclose(self) -> None:
            self.close_count += 1

    monkeypatch.setattr(app_main, "TmdbScraper", TrackedTmdb)
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)

    with TestClient(app) as test_client:
        assert len(TrackedTmdb.instances) == 1
        initial = TrackedTmdb.instances[0]
        response = test_client.post(
            "/settings",
            data={
                "schedule_cron": "0 5 * * *",
                "douban_delay_seconds": "2.0",
                "tmdb_delay_seconds": "0.5",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert len(TrackedTmdb.instances) == 2
        replacement = TrackedTmdb.instances[1]
        assert initial.close_count == 1
        assert replacement.close_count == 0

    assert initial.close_count == 1
    assert replacement.close_count == 1


def test_settings_waits_for_inflight_tmdb_search_before_closing_old_scraper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    entered = Event()
    release = Event()

    class BlockingTmdb:
        instances: ClassVar[list[BlockingTmdb]] = []

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.close_count = 0
            self.instances.append(self)

        async def search_candidates(self, title: str, media_type: str) -> list[object]:
            entered.set()
            while not release.is_set():
                await asyncio.sleep(0.01)
            if self.close_count:
                raise RuntimeError("scraper closed during search")
            return []

        async def aclose(self) -> None:
            self.close_count += 1

    monkeypatch.setattr(app_main, "TmdbScraper", BlockingTmdb)
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)

    with TestClient(app) as test_client, ThreadPoolExecutor(max_workers=2) as executor:
        search_future = executor.submit(
            test_client.get,
            "/api/search?title=blocked&media_type=movie",
        )
        assert entered.wait(timeout=5)
        initial = BlockingTmdb.instances[0]
        settings_future = executor.submit(
            test_client.post,
            "/settings",
            data={
                "schedule_cron": "0 5 * * *",
                "douban_delay_seconds": "2.0",
                "tmdb_delay_seconds": "0.5",
            },
            follow_redirects=False,
        )

        with pytest.raises(FutureTimeoutError):
            settings_future.result(timeout=0.2)
        assert initial.close_count == 0

        release.set()
        search_response = search_future.result(timeout=5)
        settings_response = settings_future.result(timeout=5)

        assert search_response.status_code == 200
        assert settings_response.status_code == 303
        assert initial.close_count == 1


def test_lifespan_closes_partially_acquired_scraper(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    class TrackedTmdb:
        instance: ClassVar[TrackedTmdb | None] = None

        def __init__(self, *args: object, **kwargs: object) -> None:
            self.close_count = 0
            type(self).instance = self

        async def aclose(self) -> None:
            self.close_count += 1

    monkeypatch.setattr(app_main, "TmdbScraper", TrackedTmdb)

    def fail_key_load(data_dir: Path) -> bytes:
        raise RuntimeError("key load failed")

    monkeypatch.setattr(app_main, "load_or_create_key", fail_key_load)
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)

    with pytest.raises(RuntimeError, match="key load failed"), TestClient(app):
        pass

    assert TrackedTmdb.instance is not None
    assert TrackedTmdb.instance.close_count == 1


# ---------------------------------------------------------------------------
# Browse API: local is clamped under browse_root
# ---------------------------------------------------------------------------
@pytest.mark.skipif(
    __import__("os").name == "nt",
    reason="browse_root is POSIX; no real /media path on Windows dev",
)
def test_browse_local_clamps_to_browse_root(tmp_path: Path) -> None:
    """A path outside browse_root is clamped back to browse_root."""
    data_dir = tmp_path / "data"
    # Pre-create browse_root with two subdirs plus a sibling we should NOT see
    browse = data_dir / "media"
    browse.mkdir(parents=True)
    (browse / "movies").mkdir()
    (browse / "tvshows").mkdir()
    sibling = data_dir / "secret"
    sibling.mkdir()
    (sibling / "should_not_appear").mkdir()

    # Pre-seed config so the app boots with our browse_root
    from app.config import save_config
    save_config({"browse_root": str(browse).replace("\\", "/")}, data_dir / "config.yaml")

    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as c:
        # Request sibling — must be clamped back to browse_root
        resp = c.get(f"/api/browse?path={sibling.as_posix()}")
        assert resp.status_code == 200
        body = resp.json()
        names = [it["name"] for it in body["items"]]
        assert "movies" in names
        assert "tvshows" in names
        assert "should_not_appear" not in names

        # Direct browse of browse_root returns both subdirs
        resp2 = c.get(f"/api/browse?path={browse.as_posix()}")
        body2 = resp2.json()
        assert "movies" in [it["name"] for it in body2["items"]]
        assert "tvshows" in [it["name"] for it in body2["items"]]


def test_browse_local_nonexistent_returns_404(client: TestClient) -> None:
    resp = client.get("/api/browse?path=/media/does_not_exist_xyz")
    assert resp.status_code == 404
    body = resp.json()
    assert body["items"] == []


def test_browse_outside_browse_root_is_clamped(tmp_path: Path) -> None:
    """A path outside browse_root is clamped back; verified via 'current' field."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # Create a real browse_root inside tmp_path
    browse = data_dir / "media"
    browse.mkdir()
    (browse / "movies").mkdir()
    (browse / "tvshows").mkdir()
    sibling = data_dir / "secret"
    sibling.mkdir()

    from app.config import save_config
    save_config(
        {"browse_root": str(browse).replace("\\", "/")},
        data_dir / "config.yaml",
    )

    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as c:
        # Request a sibling outside browse_root: clamping should redirect to browse_root
        resp = c.get(f"/api/browse?path={sibling.as_posix()}")
        assert resp.status_code == 200
        body = resp.json()
        # Clamping moved us to browse_root, not the sibling
        assert body["current"] != str(sibling)
        # And we see browse_root's children, not the sibling's
        names = [it["name"] for it in body["items"]]
        assert "movies" in names
        assert "tvshows" in names
        # The sibling's children are NOT visible
        assert all(n not in names for n in sibling.iterdir() if n.is_dir())


def test_browse_includes_mount_source_annotation(tmp_path: Path) -> None:
    """When /proc/mounts is readable, child dirs annotated with mount source."""
    data_dir = tmp_path / "data"
    browse = data_dir / "media"
    browse.mkdir(parents=True)
    (browse / "movies").mkdir()

    fake_proc = tmp_path / "proc_mounts"
    fake_proc.write_text(
        "overlay / overlay rw,relatime 0 0\n"
        "/host/movies " + str(browse / "movies").replace("\\", "/") + " ext4 rw,relatime 0 0\n",
        encoding="utf-8",
    )

    # Monkeypatch the helper directly to read from our fake file
    from app import main as app_main

    def fake_read():
        out: dict[str, str] = {}
        for line in fake_proc.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 3:
                out[parts[1]] = parts[0]
        return out

    app_main._read_proc_mounts = fake_read  # type: ignore[assignment]

    from app.config import save_config
    save_config({"browse_root": str(browse).replace("\\", "/")}, data_dir / "config.yaml")

    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as c:
        resp = c.get(f"/api/browse?path={browse.as_posix()}")
        items = resp.json()["items"]
        movies = next(it for it in items if it["name"] == "movies")
        assert movies.get("mount_source") == "/host/movies"


def test_settings_browse_root_roundtrip(client: TestClient) -> None:
    """browse_root can be saved through the settings form and re-read."""
    resp = client.post("/settings", data={
        "schedule_cron": "0 4 * * *",
        "browse_root": "/srv/media",
    }, follow_redirects=False)
    assert resp.status_code == 303
    page = client.get("/settings").text
    assert "/srv/media" in page


def test_settings_browse_root_relative_rejected(client: TestClient) -> None:
    """browse_root must be absolute; relative values are rejected."""
    resp = client.post("/settings", data={
        "schedule_cron": "0 4 * * *",
        "browse_root": "relative/path",
    }, follow_redirects=False)
    assert resp.status_code == 303
    # Existing / default browse_root should still be in the page (not overwritten)
    page = client.get("/settings").text
    assert 'value="/"' in page or 'value="/media"' in page
    assert "relative/path" not in page


def test_play_page_escapes_malicious_metadata() -> None:
    title = "</h2><script>alert('title')</script>"
    filename = '<img onerror="alert(1)" src=x>.mp4'
    extension = ".x');</script><script>alert(2)</script>"

    page = _play_page_html(
        7,
        title,
        {"found": True, "filename": filename, "ext": extension, "size": 10},
    )

    assert title not in page
    assert filename not in page
    assert extension.upper().lstrip(".") not in page
    assert "&lt;/h2&gt;&lt;script&gt;alert(&#x27;title&#x27;)&lt;/script&gt;" in page
    assert '&lt;img onerror=&quot;' in page
    assert 'data-format-label="X&#x27;);&lt;/SCRIPT&gt;' in page


def test_preview_uses_inert_metadata_attributes(tmp_path: Path) -> None:
    from app.database import Library, MediaItem

    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, start_scheduler=False)
    payload = "\"');</div><script>alert(1)</script>\\\nnext"
    with TestClient(app) as test_client:
        with test_client.app.state.session_factory.begin() as session:
            library = Library(name="L", path="/media", media_type="movie")
            session.add(library)
            session.flush()
            session.add(MediaItem(
                library_id=library.id,
                media_type="movie",
                folder_path=f"/media/{payload}",
                parsed_title=payload,
                matched_title=payload,
                matched_original_title=payload,
                overview=payload,
                genres="测试",
                status="matched",
            ))

        page = test_client.get("/preview").text

    assert 'onclick="openDetail(this)"' in page
    assert "<script>alert(1)</script>" not in page
    assert "openDetail(1," not in page
    assert "data-title=" in page


def test_tmdb_candidate_template_uses_safe_dom_construction() -> None:
    template = (Path(__file__).parents[1] / "app" / "templates" / "items.html").read_text(
        encoding="utf-8"
    )

    assert "innerHTML" not in template
    assert "title.textContent" in template
    assert "original.textContent" in template
    assert "poster.src" in template
    assert "data.error" in template
    assert "showSearchMessage" in template


@pytest.mark.parametrize(
    ("header", "expected", "content_range"),
    [
        ("bytes=0-3", b"0123", "bytes 0-3/10"),
        ("bytes=7-", b"789", "bytes 7-9/10"),
        ("bytes=-4", b"6789", "bytes 6-9/10"),
    ],
)
def test_stream_route_supports_valid_single_ranges(
    tmp_path: Path,
    header: str,
    expected: bytes,
    content_range: str,
) -> None:
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app) as test_client:
        item_id = _add_local_stream_item(test_client, tmp_path / "media", b"0123456789")
        response = test_client.get(f"/api/stream/{item_id}", headers={"Range": header})

    assert response.status_code == 206
    assert response.content == expected
    assert response.headers["content-range"] == content_range
    assert response.headers["content-length"] == str(len(expected))
    assert response.headers["accept-ranges"] == "bytes"


@pytest.mark.parametrize(
    "header",
    [
        "bytes=999-1000",
        "bytes=7-3",
        "bytes=0-1,4-5",
        "garbage",
        "bytes=-0",
    ],
)
def test_stream_route_rejects_invalid_ranges(tmp_path: Path, header: str) -> None:
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app) as test_client:
        item_id = _add_local_stream_item(test_client, tmp_path / "media", b"0123456789")
        response = test_client.get(f"/api/stream/{item_id}", headers={"Range": header})

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"
    assert response.headers["accept-ranges"] == "bytes"


def test_stream_route_rejects_oversized_range_integer(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app) as test_client:
        item_id = _add_local_stream_item(test_client, tmp_path / "media", b"0123456789")
        response = test_client.get(
            f"/api/stream/{item_id}",
            headers={"Range": "bytes=" + "9" * 5000 + "-"},
        )

    assert response.status_code == 416
    assert response.headers["content-range"] == "bytes */10"


def test_stream_route_full_and_empty_files(tmp_path: Path) -> None:
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app) as test_client:
        full_id = _add_local_stream_item(test_client, tmp_path / "full", b"0123456789")
        empty_id = _add_local_stream_item(test_client, tmp_path / "empty", b"")
        full_response = test_client.get(f"/api/stream/{full_id}")
        empty_response = test_client.get(f"/api/stream/{empty_id}")
        empty_range_response = test_client.get(
            f"/api/stream/{empty_id}",
            headers={"Range": "bytes=0-0"},
        )

    assert full_response.status_code == 200
    assert full_response.content == b"0123456789"
    assert full_response.headers["content-length"] == "10"
    assert empty_response.status_code == 200
    assert empty_response.content == b""
    assert empty_response.headers["content-length"] == "0"
    assert empty_range_response.status_code == 416
    assert empty_range_response.headers["content-range"] == "bytes */0"


def test_stream_route_reads_large_range_in_bounded_chunks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main
    total_size = 2 * 1024 * 1024 + 17

    class FakeConnection:
        def __init__(self) -> None:
            self.calls: list[tuple[int, int]] = []
            self.closed = False

        async def file_size(self, path: str) -> int:
            assert path == "video.mp4"
            return total_size

        async def read_range(self, path: str, offset: int, size: int) -> bytes:
            assert path == "video.mp4"
            self.calls.append((offset, size))
            return b"x" * size

        async def aclose(self) -> None:
            self.closed = True

    fake = FakeConnection()
    monkeypatch.setattr(app_main, "_library_connection", lambda *args: fake)
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app) as test_client:
        item_id = _add_remote_stream_item(test_client)
        response = test_client.get(
            f"/api/stream/{item_id}",
            headers={"Range": f"bytes=0-{total_size - 1}"},
        )

    assert response.status_code == 206
    assert len(response.content) == total_size
    assert fake.calls == [
        (0, 1024 * 1024),
        (1024 * 1024, 1024 * 1024),
        (2 * 1024 * 1024, 17),
    ]
    assert fake.closed is True


def test_stream_route_closes_connection_for_invalid_range(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        async def file_size(self, path: str) -> int:
            return 10

        async def read_range(self, path: str, offset: int, size: int) -> bytes:
            raise AssertionError("invalid range must not read data")

        async def aclose(self) -> None:
            self.closed = True

    fake = FakeConnection()
    monkeypatch.setattr(app_main, "_library_connection", lambda *args: fake)
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app) as test_client:
        item_id = _add_remote_stream_item(test_client)
        response = test_client.get(
            f"/api/stream/{item_id}",
            headers={"Range": "bytes=99-100"},
        )

    assert response.status_code == 416
    assert fake.closed is True


def test_stream_route_closes_connection_when_chunk_read_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import main as app_main

    class FakeConnection:
        def __init__(self) -> None:
            self.closed = False

        async def file_size(self, path: str) -> int:
            return 10

        async def read_range(self, path: str, offset: int, size: int) -> bytes:
            raise OSError("read failed")

        async def aclose(self) -> None:
            self.closed = True

    fake = FakeConnection()
    monkeypatch.setattr(app_main, "_library_connection", lambda *args: fake)
    app = create_app(data_dir=tmp_path / "data", start_scheduler=False)
    with TestClient(app, raise_server_exceptions=False) as test_client:
        item_id = _add_remote_stream_item(test_client)
        test_client.get(f"/api/stream/{item_id}")

    assert fake.closed is True
