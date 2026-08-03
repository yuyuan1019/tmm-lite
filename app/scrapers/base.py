"""Shared data classes for scraper results.

This is the **only** definition site for :class:`ScrapedMeta`.  M4 (NFO writer),
M5 (TMDB scraper), and M7 (scanner) all import from here.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ScrapedMeta:
    """Scraped metadata for a single movie or TV show.

    This dataclass is **mutable** — the douban supplement stage replaces
    ``overview`` and ``rating`` in-place.
    """

    source: str
    source_id: str
    title: str
    original_title: str | None
    year: int | None
    overview: str | None
    rating: float | None
    genres: list[str] = field(default_factory=list)
    poster_url: str | None = None
    backdrop_url: str | None = None
