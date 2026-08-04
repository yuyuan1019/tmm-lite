"""M1 configuration tests — M1-T1 through M1-T7."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from app.config import (
    ConfigError,
    load_config,
    save_config,
    validate_cron,
)


# ---------------------------------------------------------------------------
# M1-T1: First load (file does not exist) → auto-create with defaults
# ---------------------------------------------------------------------------
def test_first_load_creates_file_with_defaults(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    assert not config_path.exists()

    config = load_config(config_path)

    assert config_path.exists()
    assert config.tmdb_api_key == ""
    assert config.use_douban is True
    assert config.douban_delay_seconds == 2.0
    assert config.overwrite_existing_nfo is False
    assert config.language == "zh-CN"
    assert config.schedule_cron == "0 4 * * *"
    assert config.libraries_seed == []
    assert config.effective_tmdb_api_key == ""
    assert config.proxy == ""


# ---------------------------------------------------------------------------
# M1-T2: API key priority — YAML > env var > empty
# ---------------------------------------------------------------------------
def test_api_key_priority_yaml(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "tmdb_api_key: yaml-key\n",
        encoding="utf-8",
    )
    os.environ["TMDB_API_KEY"] = "env-key"

    config = load_config(config_path)
    assert config.tmdb_api_key == "yaml-key"
    assert config.effective_tmdb_api_key == "yaml-key"


def test_api_key_priority_env_fallback(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "tmdb_api_key: \"\"\n",
        encoding="utf-8",
    )
    os.environ["TMDB_API_KEY"] = "env-key"

    config = load_config(config_path)
    assert config.tmdb_api_key == ""
    assert config.effective_tmdb_api_key == "env-key"


def test_api_key_priority_both_empty(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "tmdb_api_key: \"\"\n",
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.effective_tmdb_api_key == ""


# ---------------------------------------------------------------------------
# M1-T3: save_config partial update — only changed key, rest preserved
# ---------------------------------------------------------------------------
def test_save_config_partial_update(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "use_douban: true\ndouban_delay_seconds: 3.0\nlanguage: zh-CN\n",
        encoding="utf-8",
    )

    config = save_config({"use_douban": False}, path=config_path)

    assert config.use_douban is False
    assert config.douban_delay_seconds == 3.0
    assert config.language == "zh-CN"

    # File on disk should match
    reloaded = load_config(config_path)
    assert reloaded.use_douban is False
    assert reloaded.douban_delay_seconds == 3.0


# ---------------------------------------------------------------------------
# M1-T3b: proxy round-trip + validation
# ---------------------------------------------------------------------------
def test_proxy_round_trip(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config = save_config({"proxy": "http://127.0.0.1:7890"}, path=config_path)
    assert config.proxy == "http://127.0.0.1:7890"

    reloaded = load_config(config_path)
    assert reloaded.proxy == "http://127.0.0.1:7890"


def test_proxy_socks5_round_trip(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config = save_config({"proxy": "socks5://127.0.0.1:1080"}, path=config_path)
    assert config.proxy == "socks5://127.0.0.1:1080"


def test_proxy_empty_ok(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config = save_config({"proxy": ""}, path=config_path)
    assert config.proxy == ""


def test_proxy_invalid_scheme_rejected(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    with pytest.raises(ConfigError, match="proxy"):
        save_config({"proxy": "ftp://127.0.0.1:21"}, path=config_path)


def test_proxy_non_string_rejected(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    with pytest.raises(ConfigError, match="proxy"):
        save_config({"proxy": 12345}, path=config_path)


# ---------------------------------------------------------------------------
# M1-T4: Atomic write — no partial state on disk
# ---------------------------------------------------------------------------
def test_atomic_write(tmp_data_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "use_douban: true\ndouban_delay_seconds: 2.0\n",
        encoding="utf-8",
    )

    original_text = config_path.read_text(encoding="utf-8")

    # Make os.replace fail to simulate crash mid-write
    def fake_replace(src: str, dst: str) -> None:
        raise OSError("simulated crash")

    monkeypatch.setattr(os, "replace", fake_replace)

    with pytest.raises(OSError, match="simulated crash"):
        save_config({"use_douban": False}, path=config_path)

    # File must be unchanged
    assert config_path.read_text(encoding="utf-8") == original_text
    # No .tmp file left behind
    tmp_path = config_path.with_suffix(config_path.suffix + ".tmp")
    assert not tmp_path.exists()


# ---------------------------------------------------------------------------
# M1-T5: Illegal cron rejected — file unchanged
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bad_cron", ["abc", "* * * *", "* * * * * * *"])
def test_illegal_cron_rejected(tmp_data_dir: Path, bad_cron: str) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "schedule_cron: '0 4 * * *'\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        save_config({"schedule_cron": bad_cron}, path=config_path)

    # File unchanged
    data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert data["schedule_cron"] == "0 4 * * *"


def test_validate_cron_rejects_four_segments() -> None:
    with pytest.raises(ConfigError, match="恰好 5 段"):
        validate_cron("* * * *")


def test_validate_cron_rejects_six_segments() -> None:
    with pytest.raises(ConfigError, match="恰好 5 段"):
        validate_cron("* * * * * *")


def test_validate_cron_accepts_valid() -> None:
    trigger = validate_cron("0 4 * * *")
    assert trigger is not None


# ---------------------------------------------------------------------------
# M1-T6: Missing / extra field tolerance
# ---------------------------------------------------------------------------
def test_missing_field_gets_default(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "language: zh-CN\n",  # missing use_douban, etc.
        encoding="utf-8",
    )

    config = load_config(config_path)
    assert config.use_douban is True  # default


def test_extra_field_preserved_in_extra(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    content = (
        "language: zh-CN\n"
        "my_custom_key: 42\n"
        "another_unknown:\n"
        "  nested: value\n"
    )
    config_path.write_text(content, encoding="utf-8")

    config = load_config(config_path)
    # Unknown keys end up in _extra
    assert "my_custom_key" in config._extra
    assert "another_unknown" in config._extra
    assert config._extra["my_custom_key"] == 42

    # save_config should preserve unknown keys
    updated = save_config({"language": "en-US"}, path=config_path)
    assert updated.language == "en-US"
    assert "my_custom_key" in updated._extra


# ---------------------------------------------------------------------------
# M1-T7: Type error protection
# ---------------------------------------------------------------------------
def test_type_error_root_not_dict(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text("- item1\n- item2\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="根节点必须是 mapping"):
        load_config(config_path)


def test_type_error_bad_delay(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "douban_delay_seconds: not_a_number\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError):
        load_config(config_path)


def test_delay_nan_rejected(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "douban_delay_seconds: .nan\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="有限数字"):
        load_config(config_path)


def test_delay_inf_rejected(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "douban_delay_seconds: .inf\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="有限数字"):
        load_config(config_path)


def test_delay_below_minimum_rejected(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "douban_delay_seconds: 0.1\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match=">= 0.5"):
        load_config(config_path)


def test_yaml_syntax_error(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "key: [unclosed\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="格式错误"):
        load_config(config_path)
    # File must be unchanged
    assert config_path.read_text(encoding="utf-8") == "key: [unclosed\n"


def test_libraries_seed_parsing(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    content = (
        "libraries:\n"
        "  - name: Movies\n"
        "    path: /media/movies\n"
        "    type: movie\n"
        "  - name: TV Shows\n"
        "    path: /media/tvshows\n"
        "    type: tv\n"
    )
    config_path.write_text(content, encoding="utf-8")

    config = load_config(config_path)
    assert len(config.libraries_seed) == 2
    assert config.libraries_seed[0].name == "Movies"
    assert config.libraries_seed[0].path == "/media/movies"
    assert config.libraries_seed[0].type == "movie"
    assert config.libraries_seed[1].type == "tv"


def test_libraries_seed_invalid_type(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text(
        "libraries:\n  - name: Bad\n    path: /x\n    type: anime\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigError, match="必须是 movie 或 tv"):
        load_config(config_path)


def test_save_config_rejects_disallowed_key(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text("use_douban: true\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="不允许通过 save_config 修改"):
        save_config({"libraries": []}, path=config_path)


def test_save_config_rejects_bad_delay(tmp_data_dir: Path) -> None:
    config_path = tmp_data_dir / "config.yaml"
    config_path.write_text("use_douban: true\n", encoding="utf-8")

    with pytest.raises(ConfigError):
        save_config({"douban_delay_seconds": -1}, path=config_path)
