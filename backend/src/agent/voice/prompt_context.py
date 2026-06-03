"""Wall-clock formatting for the voice system prompt.

Both helpers fall back to a UTC rendering when the user's timezone name is
missing or unrecognized, so a bad `timezone` field can never crash session setup.
"""

from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo


def local_time_in_zone(timezone_name: str) -> str:
    """Format current wall-clock time in the user's timezone for the prompt."""
    try:
        return datetime.now(ZoneInfo(timezone_name)).strftime("%-I:%M %p")
    except Exception:
        return datetime.now(UTC).strftime("%H:%M UTC")


def local_date_in_zone(timezone_name: str) -> str:
    """Format today's date in the user's timezone for the prompt (e.g. 'Thursday, 28 May 2026')."""
    try:
        return datetime.now(ZoneInfo(timezone_name)).strftime("%A, %-d %B %Y")
    except Exception:
        return datetime.now(UTC).strftime("%A, %-d %B %Y UTC")
