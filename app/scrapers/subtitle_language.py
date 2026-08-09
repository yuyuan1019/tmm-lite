"""Language checks shared by subtitle providers and the unified downloader."""

from __future__ import annotations

import re

_CHINESE_LANGUAGE_CODES = frozenset(
    {"chi", "zho", "zh", "zh-cn", "zh-tw", "zh-hans", "zh-hant", "ze"}
)
_MIN_HAN_CHARACTERS = 20

_SIMPLIFIED_TOKEN = re.compile(
    r"(?:^|[^a-z0-9])(?:chs|sc|zh[-_.]?(?:cn|hans)|gb)(?:$|[^a-z0-9])"
)
_TRADITIONAL_TOKEN = re.compile(
    r"(?:^|[^a-z0-9])(?:cht|tc|zh[-_.]?(?:tw|hk|hant)|big5)(?:$|[^a-z0-9])"
)
_CHINESE_TOKEN = re.compile(
    r"(?:^|[^a-z0-9])(?:chi|zho|zh)(?:$|[^a-z0-9])"
)
_NON_CHINESE_TOKEN = re.compile(
    r"(?:^|[^a-z0-9])"
    r"(?:en|eng|english|ja|jpn|japanese|ko|kor|korean|fr|fre|fra|es|spa)"
    r"(?:$|[^a-z0-9])"
)


def expects_chinese(languages: list[str]) -> bool:
    """Return whether the configured language list asks for Chinese."""
    return any(
        language.strip().lower().replace("_", "-") in _CHINESE_LANGUAGE_CODES
        for language in languages
    )


def _character_counts(text: str) -> tuple[int, int, int]:
    han = sum("\u3400" <= char <= "\u4dbf" or "\u4e00" <= char <= "\u9fff" for char in text)
    kana = sum("\u3040" <= char <= "\u30ff" or "\u31f0" <= char <= "\u31ff" for char in text)
    hangul = sum("\u1100" <= char <= "\u11ff" or "\uac00" <= char <= "\ud7af" for char in text)
    return han, kana, hangul


def _decoded_chinese_score(text: str) -> int:
    han, kana, hangul = _character_counts(text)
    if han < _MIN_HAN_CHARACTERS:
        return 0
    foreign_limit = max(5, han // 10)
    if kana >= foreign_limit or hangul >= foreign_limit:
        return 0
    return han - (kana + hangul) * 4


def _decode_subtitle_candidates(data: bytes) -> tuple[str, ...]:
    """Decode common subtitle encodings without accepting lossy output."""
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return (data.decode("utf-16"),)
        except UnicodeDecodeError:
            return ()

    try:
        # UTF-8 is decisive: trying a second East Asian codec could turn valid
        # Japanese UTF-8 into plausible-looking but meaningless Han characters.
        return (data.decode("utf-8-sig"),)
    except UnicodeDecodeError:
        pass

    # A few subtitle tools emit UTF-16 without a BOM. Only try it when the byte
    # layout contains the expected NUL pattern, avoiding binary false positives.
    sample = data[:4096]
    if sample:
        even_nuls = sample[0::2].count(0)
        odd_nuls = sample[1::2].count(0)
        pairs = max(1, len(sample) // 2)
        encoding = None
        if odd_nuls / pairs > 0.2:
            encoding = "utf-16-le"
        elif even_nuls / pairs > 0.2:
            encoding = "utf-16-be"
        if encoding is not None:
            try:
                return (data.decode(encoding),)
            except UnicodeDecodeError:
                return ()

    # Shift-JIS and CP949 can otherwise be decoded as GB18030 gibberish made
    # almost entirely of Han characters. Recognize those neighboring encodings
    # first and preserve their kana/Hangul evidence for rejection below.
    for encoding, script_index in (("cp932", 1), ("cp949", 2)):
        try:
            foreign_text = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        counts = _character_counts(foreign_text)
        if counts[script_index] >= 5:
            return (foreign_text,)

    candidates: list[str] = []
    for encoding in ("gb18030", "big5"):
        try:
            candidates.append(data.decode(encoding))
        except UnicodeDecodeError:
            continue
    return tuple(candidates)


def chinese_text_score(data: bytes) -> int:
    """Score Chinese subtitle text, rejecting Japanese and Korean payloads."""
    return max(
        (_decoded_chinese_score(text) for text in _decode_subtitle_candidates(data)),
        default=0,
    )


def contains_chinese_text(data: bytes) -> bool:
    """Return whether a subtitle payload contains substantial Chinese text."""
    return chinese_text_score(data) > 0


def filename_language_score(filename: str, languages: list[str]) -> int:
    """Rank Chinese subtitle filenames, respecting simplified/traditional preference."""
    if not expects_chinese(languages):
        return 0

    name = filename.lower()
    simplified = any(marker in name for marker in ("简体", "简中", "简体中文", "简"))
    traditional = any(marker in name for marker in ("繁体", "繁中", "繁体中文", "繁"))
    generic = any(marker in name for marker in ("中文", "中字", "中英", "双语"))
    simplified = simplified or _SIMPLIFIED_TOKEN.search(name) is not None
    traditional = traditional or _TRADITIONAL_TOKEN.search(name) is not None
    generic = generic or _CHINESE_TOKEN.search(name) is not None

    first_language = languages[0].strip().lower().replace("_", "-") if languages else "zh-cn"
    prefer_traditional = first_language in {"zh-tw", "zh-hant"}
    score = 0
    if simplified:
        score = max(score, 2 if prefer_traditional else 4)
    if traditional:
        score = max(score, 4 if prefer_traditional else 2)
    if generic:
        score = max(score, 3)
    if score == 0 and (
        _NON_CHINESE_TOKEN.search(name) is not None
        or any(marker in name for marker in ("英文", "日文", "韩文", "法文", "西班牙文"))
    ):
        return -5
    return score
