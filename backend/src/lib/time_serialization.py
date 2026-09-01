"""Shared serialization for Firestore datetime values."""

from __future__ import annotations

from datetime import UTC, datetime


def to_aware(value: datetime) -> datetime:
    """Coerce a possibly-naive datetime to aware UTC.

    Firestore client values and stored timestamps arrive naive or aware
    depending on the SDK path; comparing a naive one to ``datetime.now(UTC)``
    raises TypeError. This is the ONE home for that coercion - the repo
    accumulated a dozen private ``_aware`` copies before it existed."""
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def firestore_datetime_to_iso(value) -> str | None:
    """Firestore datetimes -> ISO 8601 strings (naive values coerced to UTC);
    anything else -> None."""
    if isinstance(value, datetime):
        return to_aware(value).isoformat()
    return None
