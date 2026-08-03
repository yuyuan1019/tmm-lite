"""TMM-Lite: Lightweight media metadata scraper."""

import os
from pathlib import Path

VIDEO_EXTENSIONS = {
    ".mkv", ".mp4", ".avi", ".ts", ".m2ts", ".mov",
    ".wmv", ".flv", ".rmvb", ".iso", ".mpg", ".mpeg",
}


def get_data_dir() -> Path:
    """Return the data directory, reading TMM_DATA_DIR env var at call time.

    Defaults to ``data`` relative to the current working directory.
    """
    return Path(os.environ.get("TMM_DATA_DIR", "data"))
