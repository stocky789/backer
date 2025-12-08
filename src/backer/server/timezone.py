"""Timezone utilities for Backer server.

This module provides timezone-aware datetime functions that respect
the user's configured timezone setting. Use these functions instead
of datetime.now() throughout the codebase to ensure consistent timestamps.
"""

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from backer.server.storage import Storage

logger = logging.getLogger(__name__)

# Module-level storage reference (set during app initialization)
_storage: "Storage | None" = None

# Cache for timezone - refreshed periodically
_cached_tz: ZoneInfo | None = None
_cache_time: float = 0
_CACHE_TTL = 60  # Refresh timezone every 60 seconds


def init_timezone(storage: "Storage") -> None:
    """Initialize the timezone module with a storage reference.

    This should be called once during app initialization.
    """
    global _storage
    _storage = storage
    # Clear cache to pick up new storage
    clear_cache()
    logger.debug("Timezone module initialized with storage")


def clear_cache() -> None:
    """Clear the timezone cache to force a refresh."""
    global _cached_tz, _cache_time
    _cached_tz = None
    _cache_time = 0


def get_timezone() -> ZoneInfo:
    """Get the configured timezone.

    Returns the user's configured timezone, or UTC if not configured
    or if storage is not initialized.
    """
    global _cached_tz, _cache_time

    now = time.time()

    # Check cache
    if _cached_tz is not None and (now - _cache_time) < _CACHE_TTL:
        return _cached_tz

    # Default to UTC if storage not initialized
    if _storage is None:
        return ZoneInfo("UTC")

    # Fetch from storage
    try:
        tz_name = _storage.get_setting("timezone", "UTC")
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
    except Exception as e:
        logger.warning(f"Failed to get timezone setting: {e}, using UTC")
        tz = ZoneInfo("UTC")

    # Update cache
    _cached_tz = tz
    _cache_time = now

    return tz


def get_now() -> datetime:
    """Get the current datetime in the configured timezone.

    This should be used instead of datetime.now() throughout the codebase
    to ensure all timestamps respect the user's timezone setting.

    Returns:
        datetime: Current time in the configured timezone (timezone-aware)
    """
    tz = get_timezone()
    return datetime.now(tz)


def get_now_naive() -> datetime:
    """Get the current datetime in the configured timezone, but without tzinfo.

    This is useful when storing timestamps in the database where we want
    the time to be in the user's timezone but don't need the tzinfo.

    Returns:
        datetime: Current time in configured timezone (naive/no tzinfo)
    """
    return get_now().replace(tzinfo=None)


def format_timestamp(dt: datetime | None = None, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    """Format a datetime using the configured timezone.

    If dt is None, uses current time.
    If dt is naive, assumes it's already in the configured timezone.
    If dt is aware, converts to the configured timezone first.

    Args:
        dt: Datetime to format (None for current time)
        fmt: strftime format string

    Returns:
        Formatted timestamp string
    """
    if dt is None:
        dt = get_now()
    elif dt.tzinfo is not None:
        # Convert to configured timezone
        dt = dt.astimezone(get_timezone())

    return dt.strftime(fmt)


def to_local(dt: datetime) -> datetime:
    """Convert a datetime to the configured timezone.

    Args:
        dt: A timezone-aware datetime

    Returns:
        datetime: The same moment in the configured timezone
    """
    if dt.tzinfo is None:
        # Assume it's UTC if naive
        dt = dt.replace(tzinfo=ZoneInfo("UTC"))
    return dt.astimezone(get_timezone())


class TimezoneFormatter(logging.Formatter):
    """A logging formatter that uses the configured timezone.

    This formatter should be used instead of the default logging.Formatter
    to ensure log timestamps respect the user's timezone setting.
    """

    converter = None  # Disable default converter

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        """Format the time using the configured timezone."""
        # Get time from record and convert to configured timezone
        ct = datetime.fromtimestamp(record.created, tz=ZoneInfo("UTC"))
        ct = ct.astimezone(get_timezone())

        if datefmt:
            return ct.strftime(datefmt)
        else:
            return ct.strftime("%Y-%m-%d %H:%M:%S")
