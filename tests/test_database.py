"""M2 database tests — M2-T1 through M2-T7."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import (
    AppMeta,
    Library,
    MediaItem,
    ScrapeLog,
    create_session_factory,
    init_db,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test.db"


def _make_library(session: Session, **kwargs: object) -> Library:
    defaults = {"name": "TestLib", "path": "/media/test", "media_type": "movie"}
    defaults.update(kwargs)
    lib = Library(**defaults)  # type: ignore[arg-type]
    session.add(lib)
    session.commit()
    return lib


def _make_item(session: Session, library_id: int, **kwargs: object) -> MediaItem:
    defaults = {
        "library_id": library_id,
        "media_type": "movie",
        "folder_path": f"/media/test/Folder_{library_id}",
        "status": "pending",
    }
    defaults.update(kwargs)
    item = MediaItem(**defaults)  # type: ignore[arg-type]
    session.add(item)
    session.commit()
    return item


# ---------------------------------------------------------------------------
# M2-T1: init_db is idempotent
# ---------------------------------------------------------------------------
def test_init_db_idempotent(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    init_db(db_path)
    init_db(db_path)  # Second call should not raise
    # Should be able to query
    from sqlalchemy import create_engine
    engine = create_engine(f"sqlite:///{db_path}")
    with Session(engine) as s:
        result = s.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        tables = [row[0] for row in result]
        assert "library" in tables
        assert "media_item" in tables
        assert "scrape_log" in tables
        assert "app_meta" in tables


# ---------------------------------------------------------------------------
# M2-T2: folder_path unique constraint
# ---------------------------------------------------------------------------
def test_folder_path_unique(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    with factory() as session:
        lib = _make_library(session)
        _make_item(session, lib.id, folder_path="/media/test/Dup")

        # Insert duplicate
        dup = MediaItem(
            library_id=lib.id,
            media_type="movie",
            folder_path="/media/test/Dup",
            status="pending",
        )
        session.add(dup)
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# M2-T3: Library.path unique constraint
# ---------------------------------------------------------------------------
def test_library_path_unique(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    with factory() as session:
        lib1 = Library(name="A", path="/same/path", media_type="movie")
        session.add(lib1)
        session.commit()

        lib2 = Library(name="B", path="/same/path", media_type="tv")
        session.add(lib2)
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# M2-T4: Cascade delete — removing Library removes its MediaItems
# ---------------------------------------------------------------------------
def test_cascade_delete(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    with factory() as session:
        lib = _make_library(session)
        _make_item(session, lib.id, folder_path="/media/test/A")
        _make_item(session, lib.id, folder_path="/media/test/B")

        # Create another library that should survive
        lib2 = _make_library(session, name="Survivor", path="/media/survivor")
        _make_item(session, lib2.id, folder_path="/media/survivor/C")

    # Delete first library
    with factory() as session:
        lib_to_delete = session.get(Library, lib.id)
        assert lib_to_delete is not None
        session.delete(lib_to_delete)
        session.commit()

    # Verify cascade
    with factory() as session:
        # Items of deleted lib are gone
        items_a = session.query(MediaItem).filter(
            MediaItem.folder_path == "/media/test/A"
        ).all()
        assert len(items_a) == 0

        # Items of surviving lib are still there
        items_b = session.query(MediaItem).filter(
            MediaItem.folder_path == "/media/survivor/C"
        ).all()
        assert len(items_b) == 1


# ---------------------------------------------------------------------------
# M2-T5: Status enum — 5 legal values, illegal rejected
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("status", ["pending", "matched", "failed", "manual_needed", "missing"])
def test_status_valid_values(tmp_path: Path, status: str) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    with factory() as session:
        lib = _make_library(session)
        item = _make_item(session, lib.id, status=status)
        assert item.status == status


def test_status_invalid_value_rejected(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    with factory() as session:
        lib = _make_library(session)
        item = MediaItem(
            library_id=lib.id,
            media_type="movie",
            folder_path="/media/test/Bad",
            status="invalid_status",
        )
        session.add(item)
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# M2-T6: Full-field MediaItem read/write round-trip
# ---------------------------------------------------------------------------
def test_media_item_full_roundtrip(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    now = datetime.now(UTC).replace(microsecond=0)

    with factory() as session:
        lib = _make_library(session)
        item = MediaItem(
            library_id=lib.id,
            media_type="movie",
            folder_path="/media/test/星际穿越 (2014)",
            parsed_title="星际穿越",
            parsed_year=2014,
            status="matched",
            source="tmdb",
            source_id="157336",
            matched_title="星际穿越",
            matched_original_title="Interstellar",
            matched_year=2014,
            overview="一部关于太空旅行的电影",
            rating=8.7,
            poster_url="https://image.tmdb.org/t/p/original/p.jpg",
            backdrop_url="https://image.tmdb.org/t/p/original/b.jpg",
            genres="科幻,冒险",
            last_scraped_at=now,
            error_message=None,
        )
        session.add(item)
        session.commit()
        item_id = item.id

    with factory() as session:
        loaded = session.get(MediaItem, item_id)
        assert loaded is not None
        assert loaded.parsed_title == "星际穿越"
        assert loaded.parsed_year == 2014
        assert loaded.status == "matched"
        assert loaded.source == "tmdb"
        assert loaded.source_id == "157336"
        assert loaded.matched_title == "星际穿越"
        assert loaded.matched_original_title == "Interstellar"
        assert loaded.matched_year == 2014
        assert loaded.overview == "一部关于太空旅行的电影"
        assert loaded.rating == 8.7
        assert loaded.genres == "科幻,冒险"
        # SQLite stores naive datetimes (no tzinfo); compare UTC values
        assert loaded.last_scraped_at is not None
        assert loaded.last_scraped_at.replace(tzinfo=None) == now.replace(tzinfo=None)
        assert loaded.error_message is None


def test_media_item_nullable_fields(tmp_path: Path) -> None:
    """Verify that nullable fields can be None."""
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    with factory() as session:
        lib = _make_library(session)
        item = MediaItem(
            library_id=lib.id,
            media_type="movie",
            folder_path="/media/test/Minimal",
            status="pending",
        )
        session.add(item)
        session.commit()
        item_id = item.id

    with factory() as session:
        loaded = session.get(MediaItem, item_id)
        assert loaded is not None
        assert loaded.parsed_title is None
        assert loaded.matched_title is None
        assert loaded.overview is None
        assert loaded.rating is None


# ---------------------------------------------------------------------------
# M2-T7: AppMeta — libraries_seed_imported survives library deletion
# ---------------------------------------------------------------------------
def test_app_meta_survives_library_deletion(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    # Set initial state
    with factory() as session:
        meta = AppMeta(key="libraries_seed_imported", value="1")
        session.add(meta)
        lib = _make_library(session)
        session.commit()

    # Delete all libraries
    with factory() as session:
        for lib in session.query(Library).all():
            session.delete(lib)
        session.commit()

    # Verify libraries are empty
    with factory() as session:
        count = session.query(Library).count()
        assert count == 0

    # AppMeta must still exist
    with factory() as session:
        meta = session.get(AppMeta, "libraries_seed_imported")
        assert meta is not None
        assert meta.value == "1"


# ---------------------------------------------------------------------------
# Foreign key PRAGMA is active
# ---------------------------------------------------------------------------
def test_foreign_key_enforcement(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    with factory() as session:
        # Try inserting MediaItem with non-existent library_id
        item = MediaItem(
            library_id=999999,
            media_type="movie",
            folder_path="/media/test/Orphan",
            status="pending",
        )
        session.add(item)
        with pytest.raises(IntegrityError):
            session.commit()


# ---------------------------------------------------------------------------
# ScrapeLog
# ---------------------------------------------------------------------------
def test_scrape_log_basic(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    now = datetime.now(UTC)

    with factory() as session:
        log = ScrapeLog(
            started_at=now,
            total=10,
            matched=8,
            failed=2,
            detail="item1: error\nitem2: error",
        )
        session.add(log)
        session.commit()

    with factory() as session:
        logs = session.query(ScrapeLog).all()
        assert len(logs) == 1
        assert logs[0].total == 10
        assert logs[0].matched == 8
        assert logs[0].failed == 2


def test_media_item_library_relationship(tmp_path: Path) -> None:
    db_path = _make_db_path(tmp_path)
    engine = init_db(db_path)
    factory = create_session_factory(engine)

    with factory() as session:
        lib = _make_library(session, name="Relationship Test", path="/media/rel")
        _make_item(session, lib.id, folder_path="/media/rel/A")

        # Access relationship
        loaded_lib = session.get(Library, lib.id)
        assert loaded_lib is not None
        assert len(loaded_lib.items) == 1
        assert loaded_lib.items[0].folder_path == "/media/rel/A"

        # Reverse relationship
        item = session.query(MediaItem).filter(
            MediaItem.folder_path == "/media/rel/A"
        ).first()
        assert item is not None
        assert item.library.name == "Relationship Test"
