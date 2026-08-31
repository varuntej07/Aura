"""Shared serialization for Firestore datetime values."""

from __future__ import annotations

from datetime import UTC, datetime


def firestore_datetime_to_iso(value) -> str | None:
    """Firestore datetimes -> ISO 8601 strings (naive values coerced to UTC);
    anything else -> None."""
    if isinstance(value, datetime):
        aware = value if value.tzinfo else value.replace(tzinfo=UTC)
        return aware.isoformat()
    return None
