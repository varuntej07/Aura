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
        now = datetime.now(ZoneInfo(timezone_name))
        hour12 = now.hour % 12 or 12
        return now.strftime(f"{hour12}:%M %p")
    except Exception:
        return datetime.now(UTC).strftime("%H:%M UTC")


def local_date_in_zone(timezone_name: str) -> str:
    """Format today's date in the user's timezone for the prompt (e.g. 'Thursday, 28 May 2026')."""
    try:
        now = datetime.now(ZoneInfo(timezone_name))
        return now.strftime(f"%A, {now.day} %B %Y")
    except Exception:
        now = datetime.now(UTC)
        return now.strftime(f"%A, {now.day} %B %Y UTC")
