"""The dashboard's time-range vocabulary, defined once.

Every range selector in the UI, every `?range=` query parameter, and every
provider that converts a range into a day count used to carry its own copy of
this table (app.py's valid-range set, panels.py's day map, and cost_provider's
day map). Adding a range meant finding each one, and a miss fails silently by
clamping to 7d.

One table, one clamp, one converter.
"""
from __future__ import annotations

# Ordered as the UI renders the segmented control.
RANGE_DAYS: dict[str, int] = {"today": 1, "7d": 7, "30d": 30}

DEFAULT_RANGE = "7d"

# The segmented-control labels the browser renders, sent with the payload so the
# UI never hardcodes its own copy either.
RANGE_KEYS: tuple[str, ...] = tuple(RANGE_DAYS)


def clamp(range_key: str) -> str:
    """Any untrusted range string -> a key this module knows. Never raises."""
    return range_key if range_key in RANGE_DAYS else DEFAULT_RANGE


def days(range_key: str) -> int:
    """Day count for a range key, clamping an unknown key to the default."""
    return RANGE_DAYS[clamp(range_key)]
