"""Canonical, explicit user-timezone resolution.

Notification timing must never silently reinterpret a user's clock as UTC. This
module owns compatibility aliases and raises a typed error so each caller can
choose the safe behavior for its product lane.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TIMEZONE_ALIASES: dict[str, str] = {
    "Asia/Calcutta": "Asia/Kolkata",
}


class TimezoneResolutionError(ValueError):
    """Raised when a user timezone is missing or unavailable in this runtime."""


@dataclass(frozen=True)
class ResolvedTimezone:
    requested_name: str
    canonical_name: str
    zone: ZoneInfo


def canonicalize_timezone_name(value: object) -> str:
    """Return a validated canonical IANA timezone name."""
    if not isinstance(value, str) or not value.strip():
        raise TimezoneResolutionError("timezone is missing")
    requested = value.strip()
    canonical = TIMEZONE_ALIASES.get(requested, requested)
    try:
        ZoneInfo(canonical)
    except ZoneInfoNotFoundError as exc:
        raise TimezoneResolutionError(f"timezone is unavailable: {requested}") from exc
    return canonical


def resolve_timezone(value: object) -> ResolvedTimezone:
    requested = value.strip() if isinstance(value, str) else ""
    canonical = canonicalize_timezone_name(value)
    return ResolvedTimezone(requested, canonical, ZoneInfo(canonical))


def localize(value: datetime, timezone_name: object) -> datetime:
    return value.astimezone(resolve_timezone(timezone_name).zone)
