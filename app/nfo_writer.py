"""NFO file generator (M4).

Writes Kodi-compatible ``movie.nfo`` and ``tvshow.nfo`` XML files using lxml.
Follows the Kodi metadata standard (Jellyfin/Emby compatible).
"""

from __future__ import annotations

import logging
import os
import re
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

    # IMDb id — used by Kodi scrapers and for exact subtitle matching
    if meta.imdb_id:
        imdb_uid = etree.SubElement(root, "uniqueid")
        imdb_uid.set("type", "imdb")
        imdb_uid.text = meta.imdb_id

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


# ---------------------------------------------------------------------------
# NFO reading — parse an existing Kodi NFO into a metadata dict
# ---------------------------------------------------------------------------


def parse_nfo(xml_bytes: bytes) -> dict[str, object] | None:
    """Parse a Kodi ``movie.nfo`` / ``tvshow.nfo`` into a metadata dict.

    The inverse of :func:`_build_nfo_bytes`: reads the fields the scanner
    writes so a folder that already ships an NFO can be loaded without hitting
    TMDB.  Tolerant of missing fields.  Returns ``None`` if the XML cannot be
    parsed or contains no recognised fields.

    Keys (all optional): ``title``, ``originaltitle``, ``year``, ``rating``,
    ``plot``, ``genres`` (list[str]), ``tmdb_id``, ``imdb_id``.
    """
    try:
        root = etree.fromstring(xml_bytes)
    except Exception:  # noqa: BLE001 (unparseable NFO — leave item unchanged)
        logger.debug("parse_nfo: failed to parse NFO XML (%d bytes)", len(xml_bytes))
        return None

    def _text(tag: str) -> str | None:
        val = root.findtext(tag)
        return val.strip() if val else None

    meta: dict[str, object] = {}

    title = _text("title")
    if title:
        meta["title"] = title
    originaltitle = _text("originaltitle")
    if originaltitle:
        meta["originaltitle"] = originaltitle

    year_raw = _text("year")
    if year_raw:
        # Year may be a range for collections (e.g. "2001-2011") — take the
        # first 4-digit group.
        m = re.search(r"\d{4}", year_raw)
        if m:
            meta["year"] = m.group(0)

    rating = _parse_rating(root)
    if rating is not None:
        meta["rating"] = rating

    plot = _text("plot")
    if plot:
        meta["plot"] = plot

    genres = [g.text.strip() for g in root.findall("genre") if g.text and g.text.strip()]
    if genres:
        meta["genres"] = genres

    tmdb_id, imdb_id = _parse_ids(root)
    if tmdb_id:
        meta["tmdb_id"] = tmdb_id
    if imdb_id:
        meta["imdb_id"] = imdb_id

    return meta or None


def _parse_rating(root: etree._Element) -> float | None:
    """Extract a numeric rating from either ``<rating>`` or ``<ratings>``."""
    el = root.find("rating")
    if el is not None and el.text:
        try:
            return float(el.text.strip())
        except ValueError:
            pass
    for r in root.findall("./ratings/rating"):
        val = r.findtext("value")
        if val:
            try:
                return float(val.strip())
            except ValueError:
                continue
    return None


def _parse_ids(root: etree._Element) -> tuple[str | None, str | None]:
    """Extract TMDB / IMDb ids from ``<uniqueid>`` (and legacy ``<tmdbid>``)."""
    tmdb_id: str | None = None
    imdb_id: str | None = None
    for uid in root.findall("uniqueid"):
        if not uid.text:
            continue
        utype = (uid.get("type") or "").lower()
        val = uid.text.strip()
        if utype == "tmdb" and not tmdb_id:
            tmdb_id = val
        elif utype == "imdb" and not imdb_id:
            imdb_id = val
    if tmdb_id is None:
        legacy = root.findtext("tmdbid")
        if legacy:
            tmdb_id = legacy.strip()
    if imdb_id is None:
        legacy = root.findtext("imdbid")
        if legacy:
            imdb_id = legacy.strip()
    return tmdb_id, imdb_id
