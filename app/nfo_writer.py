"""NFO file generator (M4).

Writes Kodi-compatible ``movie.nfo`` and ``tvshow.nfo`` XML files using lxml.
Follows the Kodi metadata standard (Jellyfin/Emby compatible).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from lxml import etree

from app.scrapers.base import ScrapedMeta

logger = logging.getLogger(__name__)


def nfo_exists(folder: Path, media_type: str) -> bool:
    """Check whether the appropriate NFO file exists in *folder*.

    ``media_type`` must be ``"movie"`` (checks ``movie.nfo``) or ``"tv"``
    (checks ``tvshow.nfo``).
    """
    if media_type == "movie":
        return (folder / "movie.nfo").is_file()
    elif media_type == "tv":
        return (folder / "tvshow.nfo").is_file()
    return False


def build_movie_nfo_bytes(meta: ScrapedMeta) -> bytes:
    """Return the XML bytes for a ``movie.nfo`` file."""
    return _build_nfo_bytes("movie", meta)


def build_tvshow_nfo_bytes(meta: ScrapedMeta) -> bytes:
    """Return the XML bytes for a ``tvshow.nfo`` file."""
    return _build_nfo_bytes("tv", meta)


def write_movie_nfo(folder: Path, meta: ScrapedMeta) -> Path:
    """Write ``movie.nfo`` to *folder* and return the file path."""
    return _write_nfo(folder, "movie", meta)


def write_tvshow_nfo(folder: Path, meta: ScrapedMeta) -> Path:
    """Write ``tvshow.nfo`` to *folder* and return the file path."""
    return _write_nfo(folder, "tv", meta)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _build_nfo_bytes(media_type: str, meta: ScrapedMeta) -> bytes:
    """Build and serialize an lxml ElementTree for the NFO."""
    root_tag = "movie" if media_type == "movie" else "tvshow"
    root = etree.Element(root_tag)

    # Title (always present)
    _add_text(root, "title", meta.title)

    # Original title
    if meta.original_title:
        _add_text(root, "originaltitle", meta.original_title)

    # Year
    if meta.year is not None:
        _add_text(root, "year", str(meta.year))

    # Rating (1 decimal place, omit if None or 0)
    if meta.rating is not None and meta.rating > 0:
        _add_text(root, "rating", f"{meta.rating:.1f}")

    # Plot / overview
    if meta.overview:
        _add_text(root, "plot", meta.overview)

    # Genres (multiple nodes)
    for genre in meta.genres:
        _add_text(root, "genre", genre)

    # Unique ID (required — must have source_id to reach this point)
    uniqueid = etree.SubElement(root, "uniqueid")
    uniqueid.set("type", "tmdb")
    uniqueid.set("default", "true")
    uniqueid.text = meta.source_id

    return etree.tostring(
        root,
        xml_declaration=True,
        encoding="UTF-8",
        standalone=True,
        pretty_print=True,
    )


def _add_text(parent: etree._Element, tag: str, text: str) -> etree._Element:
    """Add a child element with text content."""
    child = etree.SubElement(parent, tag)
    child.text = text
    return child


def _write_nfo(folder: Path, media_type: str, meta: ScrapedMeta) -> Path:
    """Build, serialize, and atomically write the NFO file."""
    xml_bytes = _build_nfo_bytes(media_type, meta)

    # Determine target path
    filename = "movie.nfo" if media_type == "movie" else "tvshow.nfo"
    target = folder / filename
    tmp = folder / (filename + ".tmp")

    folder.mkdir(parents=True, exist_ok=True)

    try:
        tmp.write_bytes(xml_bytes)
        os.replace(tmp, target)
        logger.debug("Wrote %s (%d bytes)", target, len(xml_bytes))
        return target
    except Exception:
        # Clean up temp file on failure
        if tmp.exists():
            tmp.unlink()
        raise
