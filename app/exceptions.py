"""Unified exception hierarchy for TMM-Lite.

All custom exceptions inherit from :class:`TmmError`. Import from this module only;
do not re-define these exceptions elsewhere.
"""


class TmmError(Exception):
    """Base exception for all TMM-Lite errors."""


class ConfigError(TmmError):
    """Configuration file syntax, type, or validation error."""


class TmdbError(TmmError):
    """TMDB API error (HTTP, network, or rate-limit)."""


class TmdbAuthError(TmdbError):
    """TMDB authentication failure (missing or invalid API key)."""


class TmdbRateLimitError(TmdbError):
    """TMDB 429 rate-limit exhausted after all retries."""


class ScrapeError(TmmError):
    """Item-level scrape failure (no results, empty title, etc.)."""


class ScanBusyError(TmmError):
    """A scan task is already running — reject duplicate trigger."""


class ItemNotFoundError(TmmError):
    """Requested MediaItem does not exist in the database."""
