"""Shared client-side read filters for Firestore rows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from .time_serialization import to_aware


def is_expired(value: Any, *, now: datetime) -> bool:
    """True when an ``expires_at``-style Firestore value is in the past.

    The Firestore TTL sweeper can lag up to ~72h behind the deadline, so list
    reads must drop already-expired rows client-side; this predicate is the
    one home for that retention rule (drafts and meetings both apply it). A
    naive stored datetime is coerced to UTC rather than raising - a TypeError
    inside a fail-closed read would silently empty the whole list.
    """
    if not isinstance(value, datetime):
        return False
    return to_aware(value) < now
