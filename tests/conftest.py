"""Shared pytest fixtures for TMM-Lite tests."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_data_dir(tmp_path: Path) -> Path:
    """A temporary ``data/`` directory for isolated config + database tests."""
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return data_dir


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove TMDB_API_KEY from the environment for test isolation."""
    monkeypatch.delenv("TMDB_API_KEY", raising=False)
