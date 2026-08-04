"""Filename / folder-name parser (M3).

Pure functions — no I/O, no side-effects.  Extracts title, year, season, and
episode numbers from typical media naming conventions.

See implementation spec §5 for the full algorithm.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

from app import VIDEO_EXTENSIONS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public return type
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedName:
    """Result of parsing a media folder or file name."""

    title: str | None
    year: int | None
    season: int | None
    episode: int | None


# ---------------------------------------------------------------------------
# Noise word table (§5.1)
# ---------------------------------------------------------------------------

_NOISE_WORDS: set[str] = {
    # Resolution / quality
    "1080p", "720p", "2160p", "4k", "uhd", "hdr", "hdr10", "hdr10+", "dv", "dovi",
    "10bit", "8bit", "sdr",
    # Source
    "bluray", "blu-ray", "bdrip", "brrip", "web-dl", "webdl", "webrip", "hdtv",
    "dvdrip", "remux", "hdrip", "cam",
    # Codec / audio
    "x264", "x265", "h264", "h.264", "h265", "h.265", "hevc", "avc", "av1",
    "aac", "ac3", "dts", "dts-hd", "truehd", "atmos", "ddp5.1", "dd5.1", "flac", "2audio",
    # Chinese labels
    "国语", "粤语", "国粤双语", "国语中字", "中字", "中英字幕", "简繁", "双语", "高清", "蓝光",
    "完整版", "未删减", "修复版", "重制版", "特效字幕",
}

# Sort by length descending so longer phrases match before substrings.
_NOISE_SORTED: list[str] = sorted(_NOISE_WORDS, key=len, reverse=True)

# Regex for noise word boundary replacement: boundary must be start/end of string
# or a non-alnum, non-CJK character.
# We rebuild per word to simplify — instead we do case-insensitive replacement
# with a guard regex.

# ---------------------------------------------------------------------------
# Regex patterns (compiled once)
# ---------------------------------------------------------------------------

# Season + Episode: S01E02, s01e02, S01.E02, etc.
_RE_SEASON_EPISODE = re.compile(
    r"[Ss](\d{1,2})[Ee](\d{1,3})"
)

# Season-only: "Season 01", "Season_01", "Season.01", or standalone "S01" (no E following)
_RE_SEASON_ONLY = re.compile(
    r"(?:Season[ ._]?(\d{1,2}))|(?<![a-zA-Z])[Ss](\d{1,2})(?![Ee]\d)"
)

# Episode-only: Chinese "第01集" / "第1话" pattern
_RE_EPISODE_CN = re.compile(
    r"第(\d{1,4})[集话]"
)

# Bracket-wrapped year: (2014) or （2014）
_RE_BRACKET_YEAR = re.compile(
    r"[（(](?P<year>(?:19|20)\d{2})[)）]"
)

# Bare year: 4-digit year not adjacent to other digits
_RE_BARE_YEAR = re.compile(
    r"(?<!\d)(?P<year>(?:19|20)\d{2})(?!\d)"
)

# Release group suffix: trailing "-UPPERCASE/ALPHANUM" at end of string
_RE_RELEASE_GROUP = re.compile(
    r"-(?=[A-Z0-9])[^-]*[A-Z][A-Z0-9]*$"
)

# Leading collection index in Chinese media naming: "14.奇异博士1" -> "奇异博士1".
# Matches digits + separator ONLY when the following title starts with a CJK
# character, so "50.First.Dates" (Latin) and "2012" (no separator) are untouched.
_RE_LEADING_INDEX = re.compile(
    r"^\d{1,4}[.．、_]\s*(?=[一-鿿])"
)

# Brackets containing noise (【】or [])
_RE_SQUARE_BRACKET = re.compile(r"【[^】]*】|\[[^\]]*\]")

# Chinese season-count marker in download-site names: "黑镜 Black Mirror[全7季]"
_RE_SEASON_COUNT = re.compile(r"全\d{1,3}季")

# Chars to replace with space
_RE_DOT_UNDERSCORE = re.compile(r"[._]+")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def parse_folder_name(name: str) -> ParsedName:
    """Parse a movie or TV show **folder** name.

    Returns a :class:`ParsedName` — never raises.
    """
    return _parse(name)


def parse_episode_name(name: str) -> ParsedName:
    """Parse an individual episode **file** name.

    Same algorithm, but prefers season/episode extraction.
    v1 delivers and tests this capability without persisting episode data.
    """
    return _parse(name)


# ---------------------------------------------------------------------------
# Internal implementation
# ---------------------------------------------------------------------------


def _parse(name: str) -> ParsedName:
    """Core parsing algorithm (§5.2)."""
    try:
        return _parse_impl(name)
    except Exception:
        logger.debug("Unexpected error parsing name %r", name, exc_info=True)
        return ParsedName(None, None, None, None)


def _parse_impl(name: str) -> ParsedName:
    work = name.strip()

    # Step 1: empty input
    if not work:
        return ParsedName(None, None, None, None)

    # Step 2: strip video extension from end
    work_lower = work.lower()
    for ext in sorted(VIDEO_EXTENSIONS, key=len, reverse=True):
        if work_lower.endswith(ext):
            work = work[: -len(ext)]
            break

    # Step 3: extract season/episode from full work (before year truncation)
    season: int | None = None
    episode: int | None = None

    # First try S01E02 pattern
    m_se = _RE_SEASON_EPISODE.search(work)
    if m_se:
        season = int(m_se.group(1))
        episode = int(m_se.group(2))
    else:
        # Try season-only
        m_s = _RE_SEASON_ONLY.search(work)
        if m_s:
            season = int(m_s.group(1) if m_s.group(1) else m_s.group(2))
        # Try Chinese episode
        m_e = _RE_EPISODE_CN.search(work)
        if m_e:
            episode = int(m_e.group(1))

    # Step 4: extract year
    year: int | None = None
    title_start: int = 0  # pos of year match start (title region = work[:title_start])

    m_bracket = _RE_BRACKET_YEAR.search(work)
    if m_bracket:
        year = int(m_bracket.group("year"))
        title_start = m_bracket.start()
    else:
        # Find all bare years, take the last one
        bare_matches = list(_RE_BARE_YEAR.finditer(work))
        if bare_matches:
            last = bare_matches[-1]
            year = int(last.group("year"))
            title_start = last.start()

    # Step 5: determine title region
    if year is not None:
        title_region = work[:title_start]
    else:
        title_region = work  # full string is title region

    # If removing the year-left-region makes title empty, use full string
    # (handles pure-year film names)
    if not title_region.strip():
        title_region = work

    # Step 6: remove season/episode segments from title region
    if season is not None:
        title_region = _remove_patterns(title_region, [
            rf"[Ss]{season:02d}[Ee]\d{{2,3}}",  # e.g. S01E02
            rf"Season[ ._]?{season:02d}",        # e.g. Season 01
            rf"[Ss]{season:02d}(?![Ee]\d)",       # standalone S01
        ])
    if episode is not None:
        title_region = _remove_patterns(title_region, [
            rf"第0*{episode}[集话]",  # 0* handles leading zeros like 第03集
        ])

    # Step 7: clean title region
    title = _clean_title(title_region)

    # Step 8: empty title → None
    if not title:
        return ParsedName(None, year, season, episode)

    return ParsedName(title, year, season, episode)


def _remove_patterns(text: str, patterns: list[str]) -> str:
    """Remove regex patterns from *text*, returning the stripped result."""
    for pat in patterns:
        text = re.sub(pat, "", text, flags=re.IGNORECASE)
    return text


def _clean_title(region: str) -> str | None:
    """Clean a title region string, returning None if nothing remains."""
    # Step 7a: drop a leading collection index ("14.奇异博士1" -> "奇异博士1")
    region = _RE_LEADING_INDEX.sub("", region, count=1)

    # Step 7b: remove release group suffix
    region = _RE_RELEASE_GROUP.sub("", region)

    # Step 7c: handle square brackets
    # Check if bracket content matches a noise word → remove brackets + content
    # Otherwise strip brackets keeping content
    def _handle_bracket(m: re.Match[str]) -> str:
        content = m.group(0)
        inner = content[1:-1]  # strip 【】 or []
        if inner.lower() in {w.lower() for w in _NOISE_WORDS}:
            return " "
        return " " + inner + " "

    region = _RE_SQUARE_BRACKET.sub(_handle_bracket, region)

    # Step 7d: Chinese season-count marker like "全7季" in [全7季]
    region = _RE_SEASON_COUNT.sub(" ", region)

    # Step 7e: remove noise words (longest-first, case-insensitive, boundary-aware)
    for word in _NOISE_SORTED:
        # Build a pattern that requires word boundaries on both sides
        # Boundary = start/end of string or any non-alnum, non-CJK char
        region = _replace_noise_word(region, word)

    # Step 7f: replace dots and underscores with spaces, collapse whitespace, strip
    region = _RE_DOT_UNDERSCORE.sub(" ", region)
    # Collapse multiple whitespace
    region = re.sub(r"\s+", " ", region).strip()

    return region if region else None


def _replace_noise_word(text: str, word: str) -> str:
    """Case-insensitive, boundary-aware replacement of *word* in *text*.

    Boundary is defined as start/end of string or a character that is not
    alphanumeric and not a CJK character (U+4E00–U+9FFF).
    """
    escaped = re.escape(word)
    # Use word boundaries for ASCII words and manual CJK boundaries
    pattern = re.compile(
        r"(?<![a-zA-Z0-9一-鿿])" + escaped + r"(?![a-zA-Z0-9一-鿿])",
        re.IGNORECASE,
    )
    return pattern.sub(" ", text)
