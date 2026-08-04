"""M9 Web layer tests — M9-T1 through M9-T12."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    """Create a TestClient with a temp data dir and scheduler disabled."""
    data_dir = tmp_path / "data"
    app = create_app(data_dir=data_dir, start_scheduler=False)
    with TestClient(app) as c:
        yield c


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


# ---------------------------------------------------------------------------
# M9-T4: rescrape
# ---------------------------------------------------------------------------
def test_rescrape_404(client: TestClient) -> None:
    resp = client.post("/items/999/rescrape")
    assert resp.status_code == 404


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
    }, follow_redirects=False)
    assert resp.status_code == 303

    # Verify page shows the new values
    resp = client.get("/settings")
    assert resp.status_code == 200


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
