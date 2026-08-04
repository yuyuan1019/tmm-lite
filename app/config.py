"""Configuration management (M1).

Reads and writes ``data/config.yaml``. Implements the single-source-of-truth rules:

- Settings are persisted in YAML.
- ``libraries`` seed is read once on first startup, then ignored.
- TMDB API key resolution: YAML value > ``TMDB_API_KEY`` env var > empty string.
- Atomic writes via temp-file + rename.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from apscheduler.triggers.cron import CronTrigger

from app import get_data_dir
from app.exceptions import ConfigError

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Shared validation
# ---------------------------------------------------------------------------

_CRON_SEGMENT_RE = re.compile(r"^\S+\s+\S+\s+\S+\s+\S+\s+\S+$")


def _is_absolute_path(value: str) -> bool:
    """Return True if *value* is an absolute path on the current platform.

    POSIX: starts with ``/``.
    Windows: has a drive letter prefix like ``C:\\`` or ``C:/``.
    """
    if value.startswith("/"):
        return True
    return (
        os.name == "nt"
        and len(value) >= 3
        and value[1] == ":"
        and value[2] in ("/", "\\")
    )


def validate_cron(cron: str) -> CronTrigger:
    """Validate a 5-segment cron expression and return a CronTrigger.

    This is the **only** cron validation entry point used by config save,
    application startup, and scheduler reschedule.  Raises :class:`ConfigError`
    on any invalid input.
    """
    if not _CRON_SEGMENT_RE.match(cron):
        raise ConfigError("Cron 表达式无效：需要恰好 5 段（分 时 日 月 周）")
    try:
        return CronTrigger.from_crontab(cron)
    except (ValueError, TypeError) as exc:
        raise ConfigError(f"Cron 表达式无效: {exc}") from exc


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class LibrarySeed:
    """A library entry from the config YAML seed (used only on first import)."""

    name: str
    path: str
    type: str  # "movie" | "tv"


@dataclass
class AppConfig:
    """Runtime configuration, loaded from ``data/config.yaml``.

    * ``tmdb_api_key`` stores the **raw** YAML value (may be empty).
    * ``effective_tmdb_api_key`` applies the priority: YAML > env var.
    * ``_extra`` holds unknown YAML keys so they survive round-trips.
    """

    tmdb_api_key: str = ""
    use_douban: bool = True
    douban_delay_seconds: float = 2.0
    tmdb_delay_seconds: float = 0.5  # min interval between TMDB API requests
    overwrite_existing_nfo: bool = False
    language: str = "zh-CN"
    schedule_cron: str = "0 4 * * *"
    scheduler_enabled: bool = True
    subtitle_enabled: bool = True
    opensubtitles_api_key: str = ""
    subtitle_languages: str = "chi,zho,zh"  # ISO 639-2, comma-separated
    browse_root: str = "/"  # local browse is clamped under this directory
    proxy: str = ""  # http(s)/socks5 proxy URL for TMDB; empty = no proxy
    libraries_seed: list[LibrarySeed] = field(default_factory=list)
    _extra: dict[str, object] = field(default_factory=dict, repr=False)

    @property
    def effective_tmdb_api_key(self) -> str:
        """Resolved API key: YAML value first, then ``TMDB_API_KEY`` env var."""
        return self.tmdb_api_key or os.environ.get("TMDB_API_KEY", "")


# ---------------------------------------------------------------------------
# Allowed keys for save_config
# ---------------------------------------------------------------------------

_ALLOWED_SAVE_KEYS = frozenset({
    "tmdb_api_key",
    "use_douban",
    "douban_delay_seconds",
    "tmdb_delay_seconds",
    "overwrite_existing_nfo",
    "language",
    "schedule_cron",
    "scheduler_enabled",
    "subtitle_enabled",
    "opensubtitles_api_key",
    "subtitle_languages",
    "browse_root",
    "proxy",
})

# ---------------------------------------------------------------------------
# Default values for missing / first-run fields
# ---------------------------------------------------------------------------

_DEFAULTS: dict[str, object] = {
    "tmdb_api_key": "",
    "use_douban": True,
    "douban_delay_seconds": 2.0,
    "tmdb_delay_seconds": 0.5,
    "overwrite_existing_nfo": False,
    "language": "zh-CN",
    "schedule_cron": "0 4 * * *",
    "scheduler_enabled": True,
    "subtitle_enabled": True,
    "opensubtitles_api_key": "",
    "subtitle_languages": "chi,zho,zh",
    "browse_root": "/",
    "proxy": "",
    "libraries": [],
}


def _validate_delay(value: object) -> float:
    """Return *value* as a float, raising ConfigError on NaN/Inf or < 0.5."""
    import math

    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"豆瓣请求间隔必须是数字: {value}") from exc
    if not math.isfinite(f):
        raise ConfigError(f"豆瓣请求间隔必须是有限数字: {value}")
    if f < 0.5:
        raise ConfigError(f"豆瓣请求间隔必须 >= 0.5 秒: {value}")
    return f


def _validate_tmdb_delay(value: object) -> float:
    """Return *value* as a float, raising ConfigError on NaN/Inf or < 0."""
    import math

    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"TMDB 请求间隔必须是数字: {value}") from exc
    if not math.isfinite(f):
        raise ConfigError(f"TMDB 请求间隔必须是有限数字: {value}")
    if f < 0:
        raise ConfigError(f"TMDB 请求间隔必须 >= 0 秒: {value}")
    return f


_PROXY_SCHEMES = ("http://", "https://", "socks4://", "socks5://", "socks5h://")


def _validate_proxy(value: object) -> None:
    """Validate the ``proxy`` setting; empty string means no proxy."""
    if not isinstance(value, str):
        raise ConfigError(f"proxy 必须是字符串: {type(value).__name__}")
    if not value:
        return
    if not value.lower().startswith(_PROXY_SCHEMES):
        raise ConfigError(
            "proxy 必须是 http/https/socks4/socks5 代理地址，如 http://127.0.0.1:7890"
        )


def _validate_config_values(raw: dict[str, object]) -> None:
    """Validate the types and values of known keys in *raw*. Raises ConfigError."""
    # Validate top-level structure
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 根节点必须是 mapping")

    # Validate individual fields
    for key, value in raw.items():
        if key == "tmdb_api_key":
            if not isinstance(value, str):
                raise ConfigError(f"tmdb_api_key 必须是字符串: {type(value).__name__}")
        elif key == "use_douban":
            if not isinstance(value, bool):
                raise ConfigError(f"use_douban 必须是布尔值: {type(value).__name__}")
        elif key == "douban_delay_seconds":
            _validate_delay(value)
        elif key == "tmdb_delay_seconds":
            _validate_tmdb_delay(value)
        elif key == "overwrite_existing_nfo":
            if not isinstance(value, bool):
                raise ConfigError(f"overwrite_existing_nfo 必须是布尔值: {type(value).__name__}")
        elif key == "language":
            if not isinstance(value, str) or not value:
                raise ConfigError("language 必须是非空字符串")
        elif key == "schedule_cron":
            if not isinstance(value, str):
                raise ConfigError(f"schedule_cron 必须是字符串: {type(value).__name__}")
            validate_cron(value)
        elif key == "scheduler_enabled":
            if not isinstance(value, bool):
                raise ConfigError(f"scheduler_enabled 必须是布尔值: {type(value).__name__}")
        elif key == "libraries":
            if not isinstance(value, list):
                raise ConfigError(f"libraries 必须是列表: {type(value).__name__}")
            for i, item in enumerate(value):
                if not isinstance(item, dict):
                    raise ConfigError(f"libraries[{i}] 必须是 mapping")
                lib_type = item.get("type", "")
                if lib_type not in ("movie", "tv"):
                    raise ConfigError(
                        f"libraries[{i}].type 必须是 movie 或 tv: {lib_type}"
                    )
        elif key == "browse_root":
            if not isinstance(value, str) or not value:
                raise ConfigError("browse_root 必须是非空字符串")
            if not _is_absolute_path(value):
                raise ConfigError(f"browse_root 必须是绝对路径: {value}")
        elif key == "proxy":
            _validate_proxy(value)
        # Unknown keys are silently preserved in _extra


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------


def load_config(path: Path | None = None) -> AppConfig:
    """Load configuration from *path* (default: ``<data_dir>/config.yaml``).

    * If the file does not exist, creates it with default values.
    * Missing keys are filled with defaults (the file is **not** rewritten).
    * Unknown keys are preserved in ``_extra``.
    * Syntax or type errors raise :class:`ConfigError` (fail-fast).
    """
    if path is None:
        path = get_data_dir() / "config.yaml"

    if not path.exists():
        return _create_default_config(path)

    raw_text = path.read_text(encoding="utf-8")
    try:
        raw: dict[str, object] = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"config.yaml 格式错误: {exc}") from exc

    # Allow empty file → treat as empty dict
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ConfigError("config.yaml 根节点必须是 mapping")

    _validate_config_values(raw)

    # Build seed list
    libraries_raw = raw.get("libraries", [])
    if not isinstance(libraries_raw, list):
        libraries_raw = []
    seeds = _parse_seed(libraries_raw)

    # Identify unknown keys
    known_keys = {
        "tmdb_api_key", "use_douban", "douban_delay_seconds",
        "tmdb_delay_seconds",
        "overwrite_existing_nfo", "language", "schedule_cron",
        "scheduler_enabled", "libraries",
        "subtitle_enabled", "opensubtitles_api_key", "subtitle_languages",
        "browse_root", "proxy",
    }
    extra = {k: v for k, v in raw.items() if k not in known_keys}

    config = AppConfig(
        tmdb_api_key=str(raw.get("tmdb_api_key", _DEFAULTS["tmdb_api_key"])),
        use_douban=bool(raw.get("use_douban", _DEFAULTS["use_douban"])),
        douban_delay_seconds=float(raw.get("douban_delay_seconds", _DEFAULTS["douban_delay_seconds"])),  # type: ignore[arg-type]
        tmdb_delay_seconds=float(raw.get("tmdb_delay_seconds", _DEFAULTS["tmdb_delay_seconds"])),  # type: ignore[arg-type]
        overwrite_existing_nfo=bool(raw.get("overwrite_existing_nfo", _DEFAULTS["overwrite_existing_nfo"])),
        language=str(raw.get("language", _DEFAULTS["language"])),
        schedule_cron=str(raw.get("schedule_cron", _DEFAULTS["schedule_cron"])),
        scheduler_enabled=bool(raw.get("scheduler_enabled", _DEFAULTS["scheduler_enabled"])),
        subtitle_enabled=bool(raw.get("subtitle_enabled", _DEFAULTS["subtitle_enabled"])),
        opensubtitles_api_key=str(raw.get("opensubtitles_api_key", _DEFAULTS["opensubtitles_api_key"])),
        subtitle_languages=str(raw.get("subtitle_languages", _DEFAULTS["subtitle_languages"])),
        browse_root=str(raw.get("browse_root", _DEFAULTS["browse_root"])),
        proxy=str(raw.get("proxy", _DEFAULTS["proxy"])),
        libraries_seed=seeds,
        _extra=extra,
    )
    # Re-validate delay for NaN/Inf safety
    _validate_delay(config.douban_delay_seconds)
    # Re-validate cron
    validate_cron(config.schedule_cron)
    return config


def _create_default_config(path: Path) -> AppConfig:
    """Write a default config.yaml and return the default AppConfig."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_yaml(path, dict(_DEFAULTS))
    return AppConfig(libraries_seed=[])


def _parse_seed(raw_list: list[object]) -> list[LibrarySeed]:
    """Parse the ``libraries`` seed list from YAML."""
    seeds: list[LibrarySeed] = []
    for item in raw_list:
        if not isinstance(item, dict):
            continue
        seeds.append(LibrarySeed(
            name=str(item.get("name", "")),
            path=str(item.get("path", "")),
            type=str(item.get("type", "movie")),
        ))
    return seeds


def save_config(updates: dict[str, object], path: Path | None = None) -> AppConfig:
    """Atomically update configuration keys and return the new :class:`AppConfig`.

    *updates* may only contain the six allowed setting keys (see spec §3.2).
    Libraries **cannot** be modified via this function.

    Writes to a temp file and renames, preserving unknown keys from the
    previous load cycle.
    """
    if path is None:
        path = get_data_dir() / "config.yaml"

    # Reject disallowed keys
    for key in updates:
        if key not in _ALLOWED_SAVE_KEYS:
            raise ConfigError(f"不允许通过 save_config 修改 '{key}'")

    # Load existing config (or defaults if missing)
    existing = load_config(path)

    # Merge updates into existing raw data
    raw: dict[str, object] = {
        "tmdb_api_key": existing.tmdb_api_key,
        "use_douban": existing.use_douban,
        "douban_delay_seconds": existing.douban_delay_seconds,
        "tmdb_delay_seconds": existing.tmdb_delay_seconds,
        "overwrite_existing_nfo": existing.overwrite_existing_nfo,
        "language": existing.language,
        "schedule_cron": existing.schedule_cron,
        "scheduler_enabled": existing.scheduler_enabled,
        "subtitle_enabled": existing.subtitle_enabled,
        "opensubtitles_api_key": existing.opensubtitles_api_key,
        "subtitle_languages": existing.subtitle_languages,
        "browse_root": existing.browse_root,
        "proxy": existing.proxy,
        "libraries": [{"name": s.name, "path": s.path, "type": s.type}
                       for s in existing.libraries_seed],
    }
    # Preserve unknown keys
    raw.update(existing._extra)
    # Apply updates
    raw.update(updates)

    # Validate the merged result
    _validate_config_values(raw)

    # Atomic write
    _write_yaml(path, raw)

    # Reload so the returned object is exactly what's on disk
    return load_config(path)


def _write_yaml(path: Path, data: dict[str, object]) -> None:
    """Atomically write *data* as YAML to *path*."""
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        os.replace(tmp_path, path)
    except Exception:
        # Clean up temp file on failure
        if tmp_path.exists():
            tmp_path.unlink()
        raise
