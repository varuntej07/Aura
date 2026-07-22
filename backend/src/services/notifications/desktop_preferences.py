"""Authenticated desktop notification capability and user preferences."""

from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta

from ..firebase import admin_firestore

SUBCOLLECTION = "notification_preferences"
DOCUMENT_ID = "desktop"
SCHEMA_VERSION = 1
_CACHE_TTL = timedelta(seconds=60)


@dataclass(frozen=True)
class DesktopPreferences:
    enabled: bool = False
    committed_enabled: bool = True
    proactive_enabled: bool = True
    account_enabled: bool = True


_cache: dict[str, tuple[datetime, DesktopPreferences]] = {}


def _ref(user_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection(SUBCOLLECTION)
        .document(DOCUMENT_ID)
    )


def _from_doc(data: dict) -> DesktopPreferences:
    return DesktopPreferences(
        enabled=data.get("enabled") is True,
        committed_enabled=data.get("committed_enabled") is not False,
        proactive_enabled=data.get("proactive_enabled") is not False,
        account_enabled=data.get("account_enabled") is not False,
    )


async def get(user_id: str, *, use_cache: bool = True) -> DesktopPreferences:
    now = datetime.now(UTC)
    cached = _cache.get(user_id)
    if use_cache and cached and now - cached[0] < _CACHE_TTL:
        return cached[1]

    def _read() -> DesktopPreferences:
        snap = _ref(user_id).get()
        return _from_doc(snap.to_dict() or {}) if snap.exists else DesktopPreferences()

    preferences = await asyncio.to_thread(_read)
    _cache[user_id] = (now, preferences)
    return preferences


async def update(user_id: str, preferences: DesktopPreferences) -> DesktopPreferences:
    now = datetime.now(UTC)
    document = {
        **asdict(preferences),
        "schema_version": SCHEMA_VERSION,
        "last_seen_at": now,
        "updated_at": now,
    }
    await asyncio.to_thread(_ref(user_id).set, document, merge=True)
    _cache[user_id] = (now, preferences)
    return preferences


def serialize(preferences: DesktopPreferences) -> dict[str, bool | int]:
    return {"schema_version": SCHEMA_VERSION, **asdict(preferences)}
