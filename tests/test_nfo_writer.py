"""M4 NFO writer tests — M4-T1 through M4-T8."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

from lxml import etree

from app.nfo_writer import build_movie_nfo_bytes, nfo_exists, parse_nfo, write_movie_nfo, write_tvshow_nfo
from app.scrapers.base import ScrapedMeta


def _make_meta(**overrides: object) -> ScrapedMeta:
    defaults: dict[str, object] = {
        "source": "tmdb",
        "source_id": "157336",
        "title": "星际穿越",
        "original_title": "Interstellar",
        "year": 2014,
        "overview": "一部关于太空旅行的电影",
        "rating": 8.7,
        "genres": ["科幻", "冒险"],
        "poster_url": "https://image.tmdb.org/t/p/original/p.jpg",
        "backdrop_url": "https://image.tmdb.org/t/p/original/b.jpg",
    }
    defaults.update(overrides)
    return ScrapedMeta(**defaults)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# M4-T1: Standard movie NFO
# ---------------------------------------------------------------------------
def test_standard_movie_nfo() -> None:
    meta = _make_meta()
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        nfo_path = write_movie_nfo(folder, meta)

        assert nfo_path.name == "movie.nfo"
        assert nfo_path.exists()

        tree = etree.parse(str(nfo_path))
        root = tree.getroot()
        assert root.tag == "movie"

        # Check fields
        assert _text(root, "title") == "星际穿越"
        assert _text(root, "originaltitle") == "Interstellar"
        assert _text(root, "year") == "2014"
        assert _text(root, "rating") == "8.7"
        assert _text(root, "plot") == "一部关于太空旅行的电影"

        genres = root.findall("genre")
        assert len(genres) == 2
        assert genres[0].text == "科幻"
        assert genres[1].text == "冒险"

        uniqueid = root.find("uniqueid")
        assert uniqueid is not None
        assert uniqueid.get("type") == "tmdb"
        assert uniqueid.get("default") == "true"
        assert uniqueid.text == "157336"


# ---------------------------------------------------------------------------
# M4-T2: TV show NFO
# ---------------------------------------------------------------------------
def test_tvshow_nfo() -> None:
    meta = _make_meta(title="繁花", original_title=None, year=None)
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        nfo_path = write_tvshow_nfo(folder, meta)

        tree = etree.parse(str(nfo_path))
        root = tree.getroot()
        assert root.tag == "tvshow"
        assert _text(root, "title") == "繁花"


# ---------------------------------------------------------------------------
# M4-T3: Special character escaping
# ---------------------------------------------------------------------------
def test_special_character_escaping() -> None:
    meta = _make_meta(
        overview='简介包含 <tag>, "引号", & 符号 & emoji: 🎬',
    )
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        nfo_path = write_movie_nfo(folder, meta)

        # Must be parseable XML
        tree = etree.parse(str(nfo_path))
        root = tree.getroot()
        plot = _text(root, "plot")
        assert "简介包含" in (plot or "")
        assert "<tag>" in (plot or "")


# ---------------------------------------------------------------------------
# M4-T4: Multiple genres
# ---------------------------------------------------------------------------
def test_multiple_genres() -> None:
    meta = _make_meta(genres=["科幻", "冒险", "剧情"])
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        nfo_path = write_movie_nfo(folder, meta)

        tree = etree.parse(str(nfo_path))
        genres = tree.getroot().findall("genre")
        assert len(genres) == 3
        assert genres[2].text == "剧情"


# ---------------------------------------------------------------------------
# M4-T5: None fields omitted
# ---------------------------------------------------------------------------
def test_none_fields_omitted() -> None:
    meta = _make_meta(original_title=None, year=None, rating=None, overview=None)
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        nfo_path = write_movie_nfo(folder, meta)

        tree = etree.parse(str(nfo_path))
        root = tree.getroot()
        # These nodes should not exist
        assert root.find("originaltitle") is None
        assert root.find("year") is None
        assert root.find("rating") is None
        assert root.find("plot") is None
        # Title and uniqueid always present
        assert root.find("title") is not None
        assert root.find("uniqueid") is not None


# ---------------------------------------------------------------------------
# M4-T6: uniqueid attributes
# ---------------------------------------------------------------------------
def test_uniqueid_attributes() -> None:
    meta = _make_meta(source_id="42")
    with TemporaryDirectory() as tmp:
        folder = Path(tmp)
        nfo_path = write_movie_nfo(folder, meta)

        tree = etree.parse(str(nfo_path))
        uniqueid = tree.getroot().find("uniqueid")
        assert uniqueid is not None
        assert uniqueid.get("type") == "tmdb"
        assert uniqueid.get("default") == "true"
        assert uniqueid.text == "42"


def test_imdb_uniqueid_included() -> None:
    meta = _make_meta(imdb_id="tt1375666")
    with TemporaryDirectory() as tmp:
        nfo_path = write_movie_nfo(Path(tmp), meta)
        tree = etree.parse(str(nfo_path))
        imdb_uid = tree.getroot().find("uniqueid[@type='imdb']")
        assert imdb_uid is not None
        assert imdb_uid.text == "tt1375666"


def test_imdb_uniqueid_omitted_when_absent() -> None:
    meta = _make_meta()  # no imdb_id
    with TemporaryDirectory() as tmp:
        nfo_path = write_movie_nfo(Path(tmp), meta)
        tree = etree.parse(str(nfo_path))
        assert tree.getroot().find("uniqueid[@type='imdb']") is None


# ---------------------------------------------------------------------------
# M4-T7: nfo_exists
# ---------------------------------------------------------------------------
def test_nfo_exists_movie(tmp_path: Path) -> None:
    folder = tmp_path / "test_movie"
    folder.mkdir()
    assert nfo_exists(folder, "movie") is False

    (folder / "movie.nfo").write_text("<movie/>")
    assert nfo_exists(folder, "movie") is True


def test_nfo_exists_tvshow(tmp_path: Path) -> None:
    folder = tmp_path / "test_tv"
    folder.mkdir()
    assert nfo_exists(folder, "tv") is False

    (folder / "tvshow.nfo").write_text("<tvshow/>")
    assert nfo_exists(folder, "tv") is True


# ---------------------------------------------------------------------------
# M4-T8: Overwrite existing file, no leftover temp
# ---------------------------------------------------------------------------
def test_overwrite_no_temp_leftover(tmp_path: Path) -> None:
    folder = tmp_path / "overwrite_test"
    folder.mkdir()

    # First write
    meta1 = _make_meta(title="Version 1")
    write_movie_nfo(folder, meta1)

    # Second write (overwrite)
    meta2 = _make_meta(title="Version 2")
    write_movie_nfo(folder, meta2)

    # Check content is version 2
    tree = etree.parse(str(folder / "movie.nfo"))
    assert _text(tree.getroot(), "title") == "Version 2"

    # No .tmp files left behind
    tmps = list(folder.glob("*.tmp"))
    assert len(tmps) == 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _text(element: etree._Element, tag: str) -> str | None:
    """Get text content of the first child with *tag*, or None."""
    child = element.find(tag)
    return child.text if child is not None else None


# ---------------------------------------------------------------------------
# NFO reading (parse_nfo)
# ---------------------------------------------------------------------------


def test_parse_nfo_roundtrip() -> None:
    """build_movie_nfo_bytes -> parse_nfo recovers the key fields."""
    xml = build_movie_nfo_bytes(_make_meta())
    parsed = parse_nfo(xml)
    assert parsed is not None
    assert parsed["title"] == "星际穿越"
    assert parsed["originaltitle"] == "Interstellar"
    assert parsed["year"] == "2014"
    assert parsed["rating"] == 8.7
    assert parsed["plot"] == "一部关于太空旅行的电影"
    assert parsed["genres"] == ["科幻", "冒险"]
    assert parsed["tmdb_id"] == "157336"


def test_parse_nfo_real_world_with_imdb() -> None:
    """Matches the Kodi NFO shape found in real libraries (uniqueid tmdb+imdb)."""
    xml = (
        "<movie>"
        "<title>银翼杀手2049</title>"
        "<originaltitle>Blade Runner 2049</originaltitle>"
        "<year>2017</year><rating>7.6</rating>"
        "<plot>三十年后……</plot>"
        "<genre>科幻</genre><genre>剧情</genre>"
        '<uniqueid type="tmdb" default="true">335984</uniqueid>'
        '<uniqueid type="imdb">tt1856101</uniqueid>'
        "</movie>"
    ).encode("utf-8")
    parsed = parse_nfo(xml)
    assert parsed is not None
    assert parsed["title"] == "银翼杀手2049"
    assert parsed["originaltitle"] == "Blade Runner 2049"
    assert parsed["year"] == "2017"
    assert parsed["rating"] == 7.6
    assert parsed["genres"] == ["科幻", "剧情"]
    assert parsed["tmdb_id"] == "335984"
    assert parsed["imdb_id"] == "tt1856101"


def test_parse_nfo_ratings_structure() -> None:
    """Newer <ratings><rating><value> form is parsed too."""
    xml = (
        '<movie><title>X</title>'
        '<ratings><rating name="imdb" max="10" default="true"><value>8.1</value></rating></ratings>'
        "</movie>"
    ).encode("utf-8")
    assert parse_nfo(xml) == {"title": "X", "rating": 8.1}


def test_parse_nfo_tolerates_missing_and_garbage() -> None:
    assert parse_nfo(b"<movie><title>Only</title></movie>") == {"title": "Only"}
    # Year range (collections) -> first 4-digit group
    assert parse_nfo(b"<movie><year>2001-2011</year></movie>")["year"] == "2001"
    # Unparseable XML -> None
    assert parse_nfo(b"not xml <<") is None
    # No recognised fields -> None
    assert parse_nfo(b"<movie></movie>") is None
