"""Unified subtitle downloader with provider fallback and explicit failures."""

from __future__ import annotations

import logging
import posixpath
from collections.abc import Callable
from io import BytesIO
from pathlib import Path, PurePosixPath
from zipfile import BadZipFile, ZipFile, is_zipfile

from app.connection import Connection
from app.exceptions import SubtitleError, TmmError
from app.scrapers.assrt import AssrtScraper
from app.scrapers.opensubtitles import DEFAULT_USER_AGENT, OpenSubtitlesScraper, SubtitleResult
from app.scrapers.subdl import SubDLScraper
from app.scrapers.subtitle_language import (
    chinese_text_score,
    chinese_variant,
    contains_chinese_text,
    expects_chinese,
    filename_language_score,
    preferred_variant,
)

logger = logging.getLogger(__name__)

_ProgressCallback = Callable[[str], None]

_SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub"})
_MAX_ARCHIVE_ENTRIES = 100
_MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
_MAX_ARCHIVE_SCAN_BYTES = 25 * 1024 * 1024

# Legacy ISO 639-2 values are accepted and translated for modern provider APIs.
_ISO_639_2_TO_1 = {
    "eng": "en",
    "jpn": "ja",
    "kor": "ko",
    "fre": "fr",
    "fra": "fr",
    "ger": "de",
    "deu": "de",
    "spa": "es",
    "por": "pt-pt",
    "rus": "ru",
    "ara": "ar",
    "hin": "hi",
    "tha": "th",
    "vie": "vi",
}


def _subtitle_extension(filename: str) -> str:
    """Return a supported subtitle extension, defaulting to ``.srt``."""
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    return suffix if suffix in _SUBTITLE_EXTENSIONS else ".srt"


def variant_label(variant: str) -> str:
    """Return a human-readable Chinese label for a detected variant."""
    return "繁体中文" if variant == "traditional" else "简体中文"


_PROVIDER_LABELS = {
    "assrt": "ASSRT",
    "opensubtitles": "OpenSubtitles",
    "subdl": "SubDL",
}


def _provider_label(provider: str) -> str:
    """Return the display name for a provider key."""
    return _PROVIDER_LABELS.get(provider, provider)


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _opensubtitles_languages(languages: list[str]) -> str:
    """Translate user/legacy language values to OpenSubtitles language codes."""
    normalized: list[str] = []
    for raw in languages:
        code = raw.strip().lower().replace("_", "-")
        if code in {"chi", "zho", "zh"}:
            normalized.extend(("zh-cn", "zh-tw", "ze"))
        elif code in {"zh-cn", "zh-tw", "ze"}:
            normalized.append(code)
        else:
            normalized.append(_ISO_639_2_TO_1.get(code, code))
    return ",".join(_unique(normalized)[:3])


def _subdl_languages(languages: list[str]) -> str:
    """Translate user/legacy language values to SubDL's upper-case codes.

    SubDL distinguishes simplified/traditional Chinese via ``ZH-Hans`` /
    ``ZH-Hant``; emit the requested variant so the provider biases its results
    accordingly, rather than collapsing every Chinese code to ``ZH``.
    """
    normalized: list[str] = []
    for raw in languages:
        code = raw.strip().lower().replace("_", "-")
        if code in {"chi", "zho", "zh"}:
            normalized.append("ZH-Hans" if preferred_variant(languages) != "traditional" else "ZH-Hant")
        elif code in {"zh-cn", "zh-hans"}:
            normalized.append("ZH-Hans")
        elif code in {"zh-tw", "zh-hant"}:
            normalized.append("ZH-Hant")
        elif code == "ze":
            normalized.append("ZH-Hans")
        else:
            normalized.append(_ISO_639_2_TO_1.get(code, code).split("-")[0].upper())
    return ",".join(_unique(normalized)[:3])


def _subtitle_suffix(language: str) -> str:
    code = language.strip().lower().replace("_", "-")
    if (
        code in {"chi", "zho", "zh", "zh-cn", "zh-tw", "ze"}
        or any(marker in code for marker in ("中文", "简体", "繁体", "中英", "双语"))
    ):
        return "zh"
    if code in {"eng", "en"}:
        return "en"
    return _ISO_639_2_TO_1.get(code, code).split("-")[0] or "zh"


def _unpack_subtitle(
    data: bytes,
    filename: str,
    preferred_languages: list[str] | None = None,
) -> tuple[bytes, str]:
    """Return a usable subtitle payload, extracting safe ZIP members in memory."""
    languages = preferred_languages or []
    require_chinese = expects_chinese(languages)
    pref_variant = preferred_variant(languages)
    stream = BytesIO(data)
    if not is_zipfile(stream):
        return data, filename
    stream.seek(0)
    try:
        with ZipFile(stream) as archive:
            members = [
                info
                for info in archive.infolist()[:_MAX_ARCHIVE_ENTRIES]
                if not info.is_dir()
                and PurePosixPath(info.filename.replace("\\", "/")).suffix.lower()
                in _SUBTITLE_EXTENSIONS
                and 0 < info.file_size <= _MAX_SUBTITLE_BYTES
                and not (info.flag_bits & 0x1)
            ]
            if not members:
                raise SubtitleError("字幕压缩包中没有可用的字幕文件")
            extension_priority = {".ass": 0, ".ssa": 1, ".srt": 2, ".vtt": 3, ".sub": 4}
            ordered_members = sorted(
                members,
                key=lambda info: (
                    -filename_language_score(info.filename, languages),
                    extension_priority.get(
                        PurePosixPath(info.filename.replace("\\", "/")).suffix.lower(),
                        99,
                    ),
                    info.filename.lower(),
                ),
            )
            scanned_bytes = 0
            for member in ordered_members:
                if scanned_bytes and scanned_bytes + member.file_size > _MAX_ARCHIVE_SCAN_BYTES:
                    continue
                payload = archive.read(member)
                scanned_bytes += member.file_size
                if not payload:
                    continue
                if not require_chinese:
                    return payload, member.filename
                if not contains_chinese_text(payload):
                    continue
                # Prefer the requested simplified/traditional variant of the
                # *content*, not just the member filename.
                if pref_variant is not None:
                    variant = chinese_variant(payload)
                    if variant is not None and variant != pref_variant:
                        continue
                return payload, member.filename
            if require_chinese:
                raise SubtitleError("字幕压缩包中没有检测到中文正文")
            raise SubtitleError("字幕压缩包中的字幕文件为空")
    except (BadZipFile, RuntimeError) as exc:
        raise SubtitleError("字幕压缩包损坏或无法读取") from exc


def _normalized_posix_path(path: str | Path | PurePosixPath) -> PurePosixPath:
    """Normalize local or remote path syntax without resolving the filesystem."""
    return PurePosixPath(posixpath.normpath(str(path).replace("\\", "/")))


class SubtitleDownloader:
    """Download subtitles through ASSRT, OpenSubtitles, then SubDL.

    Args:
        opensubtitles_api_key: API key for opensubtitles.com (may be empty).
        preferred_languages: Comma-separated language codes, default ``zh-cn``.
    """

    def __init__(
        self,
        opensubtitles_api_key: str,
        preferred_languages: str = "zh-cn",
        assrt_token: str = "",
        subdl_api_key: str = "",
        proxy: str = "",
        on_progress: Callable[[str], None] | None = None,
    ) -> None:
        self._os_key = opensubtitles_api_key
        self._assrt_token = assrt_token
        self._subdl_api_key = subdl_api_key
        self._proxy = proxy
        self._on_progress = on_progress
        self._languages = [l.strip() for l in preferred_languages.split(",") if l.strip()]
        if not self._languages:
            self._languages = ["zh-cn"]

        # Lazy-initialised
        self._assrt: AssrtScraper | None = None
        self._os: OpenSubtitlesScraper | None = None
        self._subdl: SubDLScraper | None = None

    def _emit(self, message: str) -> None:
        """Forward a human-readable progress line to the live-log callback."""
        if self._on_progress is not None:
            self._on_progress(f"  {message}")

    async def download(
        self,
        title: str,
        year: int | None,
        media_folder: Path,
        video_filename: str | None = None,
        imdb_id: str | None = None,
        connection: Connection | None = None,
    ) -> Path | None:
        """Download the best-matching subtitle to *media_folder*.

        Returns the local path to the downloaded file, or ``None``.
        When *connection* is provided, the subtitle is written through it
        instead of to the local filesystem.
        """
        assrt_languages = ",".join(self._languages[:3])
        opensubtitles_languages = _opensubtitles_languages(self._languages)
        subdl_languages = _subdl_languages(self._languages)
        attempted = 0
        failures: list[str] = []

        # Try ASSRT first (Chinese-focused — best hit rate for zh subtitles)
        if self._assrt_token:
            attempted += 1
            self._emit("尝试字幕源 ASSRT")
            try:
                result = await self._search_assrt(title, year, assrt_languages)
                if result is not None:
                    self._emit(f"ASSRT 命中候选: {result.filename}")
                    return await self._save(result, media_folder, video_filename, connection)
                self._emit("ASSRT 未找到匹配")
                logger.info("ASSRT: 无结果 %s (%s)", title, year)
            except TmmError as exc:
                failures.append(f"ASSRT: {exc}")
                self._emit(f"ASSRT 失败: {exc}")
                logger.warning("ASSRT failed, trying OpenSubtitles: %s", exc)

        # Try OpenSubtitles
        if self._os_key:
            attempted += 1
            self._emit("尝试字幕源 OpenSubtitles")
            try:
                result = await self._search_os(
                    title, year, opensubtitles_languages, imdb_id
                )
                if result is not None:
                    self._emit(f"OpenSubtitles 命中候选: {result.filename}")
                    return await self._save(result, media_folder, video_filename, connection)
                self._emit("OpenSubtitles 未找到匹配")
                logger.info("OpenSubtitles: 无结果 %s (%s, imdb=%s)", title, year, imdb_id)
            except TmmError as exc:
                failures.append(f"OpenSubtitles: {exc}")
                self._emit(f"OpenSubtitles 失败: {exc}")
                logger.warning("OpenSubtitles failed, trying SubDL: %s", exc)

        # Fallback to SubDL
        if self._subdl_api_key:
            attempted += 1
            self._emit("尝试字幕源 SubDL")
            try:
                result = await self._search_subdl(title, year, subdl_languages, imdb_id)
                if result is not None:
                    self._emit(f"SubDL 命中候选: {result.filename}")
                    return await self._save(result, media_folder, video_filename, connection)
                self._emit("SubDL 未找到匹配")
                logger.info("SubDL: 无结果 %s (%s)", title, year)
            except TmmError as exc:
                failures.append(f"SubDL: {exc}")
                self._emit(f"SubDL 失败: {exc}")
                logger.warning("SubDL failed, no subtitles downloaded: %s", exc)

        if attempted == 0:
            raise SubtitleError("未配置可用字幕源，请填写 ASSRT、OpenSubtitles 或 SubDL 密钥")
        if failures:
            raise SubtitleError("字幕源请求失败：" + "；".join(failures))

        self._emit("所有字幕源均未返回可用结果")
        logger.info("未下载到字幕: %s (%s, imdb=%s)", title, year, imdb_id)
        return None

    async def aclose(self) -> None:
        if self._assrt is not None:
            await self._assrt.aclose()
            self._assrt = None
        if self._os is not None:
            await self._os.aclose()
            self._os = None
        if self._subdl is not None:
            await self._subdl.aclose()
            self._subdl = None

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _search_os(
        self, title: str, year: int | None, languages: str, imdb_id: str | None,
    ) -> SubtitleResult | None:
        if self._os is None:
            self._os = OpenSubtitlesScraper(
                self._os_key, DEFAULT_USER_AGENT, proxy=self._proxy
            )
        results = await self._os.search(title, year, languages, imdb_id)
        if not results and imdb_id:
            results = await self._os.search(title, year, languages, None)
        # Prefer non-HI (hearing impaired) results, then the requested
        # simplified/traditional variant as indicated by the candidate
        # filename — avoids downloading a wrong-variant file only to reject
        # it after the content check.
        results = sorted(
            results,
            key=lambda r: (
                1 if r.hearing_impaired else 0,
                -filename_language_score(r.filename, self._languages),
            ),
        )
        return results[0] if results else None

    async def _search_assrt(
        self, title: str, year: int | None, languages: str,
    ) -> SubtitleResult | None:
        if self._assrt is None:
            self._assrt = AssrtScraper(self._assrt_token, proxy=self._proxy)
        results = await self._assrt.search(title, year, languages)
        return results[0] if results else None

    async def _search_subdl(
        self,
        title: str,
        year: int | None,
        languages: str,
        imdb_id: str | None,
    ) -> SubtitleResult | None:
        if self._subdl is None:
            self._subdl = SubDLScraper(self._subdl_api_key, proxy=self._proxy)
        results = await self._subdl.search(title, year, languages, imdb_id)
        if not results and imdb_id:
            results = await self._subdl.search(title, year, languages, None)
        # Prefer the requested simplified/traditional variant (SubDL unpacks
        # multi-language bundles into per-variant candidates) so a wrong-variant
        # file is not downloaded only to be rejected by the content check.
        results = sorted(
            results,
            key=lambda r: -filename_language_score(r.filename, self._languages),
        )
        return results[0] if results else None

    async def _save(
        self,
        result: SubtitleResult,
        folder: Path,
        video_filename: str | None = None,
        connection: Connection | None = None,
    ) -> Path:
        """Download and save the subtitle file."""
        folder_path = _normalized_posix_path(folder)

        # Prefer the video stem and keep a nested video's relative directory.
        if video_filename:
            video_path = _normalized_posix_path(video_filename)
            if video_path.is_absolute() or ".." in video_path.parts:
                raise ValueError("video_filename must be relative to the media folder")
            stem = video_path.stem
            dest_folder = _normalized_posix_path(folder_path / video_path.parent)
        else:
            stem = "subtitles"
            dest_folder = folder_path

        # Reject an out-of-root destination before making a provider download request.
        root_path: PurePosixPath | None = None
        if connection is not None:
            root_path = _normalized_posix_path(connection.root)
            try:
                dest_folder.relative_to(root_path)
            except ValueError as exc:
                raise ValueError(
                    f"Subtitle destination {dest_folder} is outside connection root {root_path}",
                ) from exc

        if result.provider == "opensubtitles":
            if self._os is None:
                self._os = OpenSubtitlesScraper(
                    self._os_key, DEFAULT_USER_AGENT, proxy=self._proxy
                )
            data = await self._os.download(result)
        elif result.provider == "assrt":
            if self._assrt is None:
                self._assrt = AssrtScraper(self._assrt_token, proxy=self._proxy)
            data = await self._assrt.download(result, self._languages)
        elif result.provider == "subdl":
            if self._subdl is None:
                self._subdl = SubDLScraper(self._subdl_api_key, proxy=self._proxy)
            data = await self._subdl.download(result.download_url)
        else:
            raise SubtitleError(f"不支持的字幕源: {result.provider}")

        if not data:
            raise SubtitleError(f"{result.provider} 返回了空字幕文件")
        data, payload_filename = _unpack_subtitle(data, result.filename, self._languages)
        if len(data) > _MAX_SUBTITLE_BYTES:
            raise SubtitleError("字幕文件过大，已拒绝保存")
        if expects_chinese(self._languages) and not contains_chinese_text(data):
            self._emit(f"{_provider_label(result.provider)} 返回的字幕不含中文，跳过")
            raise SubtitleError(
                f"{result.provider} 返回的字幕实际不含中文，已拒绝保存并尝试下一个字幕源"
            )
        # Verify the *content* matches the requested simplified/traditional
        # variant — a traditional-Chinese subtitle labelled as Chinese would
        # otherwise pass the language check above.
        pref_variant = preferred_variant(self._languages)
        if expects_chinese(self._languages) and pref_variant is not None:
            variant = chinese_variant(data)
            if variant is not None and variant != pref_variant:
                self._emit(
                    f"{_provider_label(result.provider)} 返回的字幕为{variant_label(variant)}，"
                    f"不符合要求的{variant_label(pref_variant)}，跳过"
                )
                raise SubtitleError(
                    f"{result.provider} 返回的字幕为{variant_label(variant)}，"
                    f"不符合要求的{variant_label(pref_variant)}，已拒绝保存并尝试下一个字幕源"
                )

        logger.debug(
            "Subtitle language verified: provider=%s, filename=%s, chinese_score=%d",
            result.provider,
            payload_filename,
            chinese_text_score(data),
        )

        # Provider labels such as "英 简 繁 法 西 日 韩" describe a bundle, not
        # the extracted member. Once Chinese content has been verified, use a
        # stable local Chinese suffix instead of trusting that bundle label.
        suffix = "zh" if expects_chinese(self._languages) else _subtitle_suffix(result.language)
        extension = _subtitle_extension(payload_filename)
        dest_path = _normalized_posix_path(dest_folder / f"{stem}.{suffix}{extension}")
        dest = Path(dest_path.as_posix())

        # Connections always consume paths relative to their configured root.
        rel_path: str | None = None
        if root_path is not None:
            rel_path = dest_path.relative_to(root_path).as_posix()

        if connection is not None and rel_path is not None:
            await connection.write_bytes(rel_path, data)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
        self._emit(f"保存字幕文件: {dest.name}")
        logger.info("Subtitle saved: %s", dest)
        return dest
