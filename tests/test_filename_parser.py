"""M3 filename parser tests — M3-T1 through M3-T13 + edge cases."""

from __future__ import annotations

import pytest

from app.parsers.filename_parser import ParsedName, parse_episode_name, parse_folder_name

# ---------------------------------------------------------------------------
# M3-T1 through M3-T13
# ---------------------------------------------------------------------------

_PARAMS = [
    # M3-T1
    ("星际穿越 (2014)", ParsedName("星际穿越", 2014, None, None)),
    # M3-T2
    ("Interstellar.2014.1080p.BluRay.x264-GROUP", ParsedName("Interstellar", 2014, None, None)),
    # M3-T3: 2012 (2009) — film named "2012" from 2009, bracket year takes priority
    ("2012 (2009)", ParsedName("2012", 2009, None, None)),
    # M3-T4: 1917 (2019) — film named "1917" from 2019
    ("1917 (2019)", ParsedName("1917", 2019, None, None)),
    # M3-T5: no bracket, last bare year
    ("The.Wandering.Earth.2019", ParsedName("The Wandering Earth", 2019, None, None)),
    # M3-T6: season + episode from file name
    ("繁花.S01E01.mkv", ParsedName("繁花", None, 1, 1)),
    # M3-T7: Chinese episode marker
    ("某剧 第03集.mp4", ParsedName("某剧", None, None, 3)),
    # M3-T8: Chinese "话" episode marker
    ("某剧 第5话.mp4", ParsedName("某剧", None, None, 5)),
    # M3-T9: "Season 02" subfolder
    ("Season 02", ParsedName(None, None, 2, None)),
    # M3-T10: pure noise → no title
    ("1080p.x264-GROUP", ParsedName(None, None, None, None)),
    # M3-T11: number in title not stripped
    ("流浪地球2 (2023)", ParsedName("流浪地球2", 2023, None, None)),
    # M3-T12: Chinese noise labels
    ("国语中字.某电影.2020.WEB-DL", ParsedName("某电影", 2020, None, None)),
    # M3-T13: full combo: title + year + season + episode
    ("繁花.2023.S01E01.mkv", ParsedName("繁花", 2023, 1, 1)),
    # M3-T14: leading collection index "14." stripped (Chinese title follows)
    ("14.奇异博士1(2016).Doctor Strange 2016 UHD BluRay REMUX 2160p HEVC Atmos TrueHD 7.1-PTer",
     ParsedName("奇异博士1", 2016, None, None)),
    # M3-T15: leading index with underscore separator
    ("12_三国演义 (1994)", ParsedName("三国演义", 1994, None, None)),
    # M3-T16: Latin title keeps leading number (not an index)
    ("50.First.Dates (2004)", ParsedName("50 First Dates", 2004, None, None)),
    # M3-T17: "007" prefix kept (colon separator, not an index)
    ("007：大破天幕杀机 (2012)", ParsedName("007：大破天幕杀机", 2012, None, None)),
]


@pytest.mark.parametrize("input_name,expected", _PARAMS)
def test_parse_folder_name(input_name: str, expected: ParsedName) -> None:
    result = parse_folder_name(input_name)
    assert result.title == expected.title, f"title mismatch: {result.title!r} != {expected.title!r}"
    assert result.year == expected.year, f"year mismatch: {result.year} != {expected.year}"
    assert result.season == expected.season, f"season mismatch: {result.season} != {expected.season}"
    assert result.episode == expected.episode, f"episode mismatch: {result.episode} != {expected.episode}"


# ---------------------------------------------------------------------------
# Additional edge cases
# ---------------------------------------------------------------------------


def test_empty_string() -> None:
    result = parse_folder_name("")
    assert result.title is None
    assert result.year is None


def test_whitespace_only() -> None:
    result = parse_folder_name("   ")
    assert result.title is None
    assert result.year is None


def test_dots_only() -> None:
    result = parse_folder_name(".....")
    assert result.title is None


def test_noise_with_numbers() -> None:
    result = parse_folder_name("1080p.AC3.x264")
    assert result.title is None


def test_release_group_stripped() -> None:
    result = parse_folder_name("Movie.Name.2020.x264-CMCT")
    assert result.title == "Movie Name"
    assert result.year == 2020


def test_full_width_brackets() -> None:
    result = parse_folder_name("片名（2018）")
    assert result.title == "片名"
    assert result.year == 2018


def test_title_with_dashes() -> None:
    """Dashes in titles should survive cleaning unless they're release group suffixes."""
    result = parse_folder_name("Spider-Man.Into.the.Spider-Verse.2018")
    assert result.title is not None
    assert "Spider" in result.title
    assert result.year == 2018


def test_parse_does_not_raise_on_garbage() -> None:
    """Any string input must not raise an exception."""
    garbage_inputs = [
        "\x00\x01\x02",
        "a" * 1000,
        "   " * 100,
        "\U0001f4a9" * 10,
    ]
    for s in garbage_inputs:
        result = parse_folder_name(s)
        assert isinstance(result, ParsedName)


def test_parse_episode_name_basic() -> None:
    result = parse_episode_name("Show.Name.S01E05.1080p.mkv")
    assert result.season == 1
    assert result.episode == 5


def test_no_year_field() -> None:
    """File with season/episode but no year."""
    result = parse_folder_name("某剧.S02E03.1080p.WEB-DL")
    assert result.season == 2
    assert result.episode == 3


def test_bracket_noise_removal() -> None:
    """【高清】around non-noise content → keep content."""
    result = parse_folder_name("【高清】星际穿越.2014.国语中字")
    assert result.title == "星际穿越"
    assert result.year == 2014


def test_multiple_years_bare() -> None:
    """When there are multiple bare years, last one wins."""
    result = parse_folder_name("Movie.2020.2021")  # two years
    assert result.year == 2021


def test_multiple_years_bracket_first() -> None:
    """Bracket year takes priority over bare year."""
    result = parse_folder_name("2012 (2009)")
    assert result.year == 2009
    assert result.title == "2012"
