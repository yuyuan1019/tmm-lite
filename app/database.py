"""Database layer (M2).

SQLAlchemy 2.0 Declarative models and session management for SQLite.

Key constraints enforced at the model / DDL level:

- ``Library.path`` unique.
- ``MediaItem.folder_path`` unique (idempotency guard).
- ``MediaItem.status`` CHECK constraint (5 legal values).
- ``library_id`` foreign key with CASCADE delete.
- Foreign keys enabled via per-connection PRAGMA.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import (
    REAL,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    create_engine,
    event,
)
from sqlalchemy.orm import (
    DeclarativeBase,
    Mapped,
    Session,
    mapped_column,
    relationship,
    sessionmaker,
)

if TYPE_CHECKING:
    from sqlalchemy import Engine

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Base
# ---------------------------------------------------------------------------

# Recommended for SQLite: sane naming convention
convention = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}
metadata = MetaData(naming_convention=convention)


class Base(DeclarativeBase):
    metadata = metadata


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class Library(Base):
    __tablename__ = "library"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    path: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    media_type: Mapped[str] = mapped_column(
        String(10), nullable=False,
    )
    connection_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="local",
    )
    connection_config_encrypted: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "media_type IN ('movie', 'tv')",
            name="ck_library_media_type",
        ),
        CheckConstraint(
            "connection_type IN ('local','ssh','webdav','smb')",
            name="ck_library_connection_type",
        ),
    )

    # Relationship: cascade delete
    items: Mapped[list[MediaItem]] = relationship(
        "MediaItem", back_populates="library", cascade="all, delete-orphan",
    )


class MediaItem(Base):
    __tablename__ = "media_item"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    library_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("library.id", ondelete="CASCADE"), nullable=False,
    )
    media_type: Mapped[str] = mapped_column(
        String(10), nullable=False,
    )
    folder_path: Mapped[str] = mapped_column(
        String(1000), nullable=False, unique=True,
    )
    parsed_title: Mapped[str | None] = mapped_column(String(500))
    parsed_year: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
    )
    source: Mapped[str | None] = mapped_column(String(20))
    source_id: Mapped[str | None] = mapped_column(String(50))
    matched_title: Mapped[str | None] = mapped_column(String(500))
    matched_original_title: Mapped[str | None] = mapped_column(String(500))
    matched_year: Mapped[int | None] = mapped_column(Integer)
    overview: Mapped[str | None] = mapped_column(Text)
    rating: Mapped[float | None] = mapped_column(REAL)
    poster_url: Mapped[str | None] = mapped_column(String(1000))
    backdrop_url: Mapped[str | None] = mapped_column(String(1000))
    genres: Mapped[str | None] = mapped_column(String(500))  # comma-separated
    last_scraped_at: Mapped[datetime | None] = mapped_column(DateTime)
    error_message: Mapped[str | None] = mapped_column(Text)

    __table_args__ = (
        CheckConstraint(
            "status IN ('pending','matched','failed','manual_needed','missing')",
            name="ck_media_item_status",
        ),
        CheckConstraint(
            "media_type IN ('movie','tv')",
            name="ck_media_item_media_type",
        ),
        Index("ix_media_item_status", "status"),
        Index("ix_media_item_library", "library_id"),
    )

    # Relationship
    library: Mapped[Library] = relationship("Library", back_populates="items")


class ScrapeLog(Base):
    __tablename__ = "scrape_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    matched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    detail: Mapped[str | None] = mapped_column(Text)


class AppMeta(Base):
    __tablename__ = "app_meta"

    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)


# ---------------------------------------------------------------------------
# Engine & Session factory
# ---------------------------------------------------------------------------


def _set_sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
    """Enable foreign keys on every new SQLite connection."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


def init_db(db_path: Path) -> Engine:
    """Create the SQLAlchemy engine and run ``CREATE TABLE IF NOT EXISTS``.

    Idempotent — safe to call multiple times.  Returns the engine.
    """
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
    )
    event.listen(engine, "connect", _set_sqlite_pragma)
    Base.metadata.create_all(engine)
    logger.info("Database initialised at %s", db_path)
    return engine


def create_session_factory(
    engine: Engine,
) -> sessionmaker[Session]:
    """Return a ``sessionmaker`` with ``expire_on_commit=False``."""
    return sessionmaker(bind=engine, expire_on_commit=False)
