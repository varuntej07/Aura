"""Presence + negative-signal state — what the surface-aware Delivery Arbiter reads.

Two behavioral facts, both written from the client ``/events`` stream and both read
by the funnel drain:

  * FOREGROUND — the user opened the app moments ago, so they are almost certainly
    looking at it. A proactive PUSH then is redundant and slightly rude; hold it and
    let it re-compete when they leave (nothing is lost).
  * DISMISS STREAK — the user has swiped away several notifications in a row without
    opening one. Buddy reads the room and goes quiet (holds proactive) until the next
    tap or app open resets the streak. This is the "negative signal" trigger family.

Both gates HOLD (never drop) and the funnel reads them fail-open, so a presence read
error never silences a notification. Committed sends (reminder/calendar/tracking)
never reach this gate — only the proactive lane does.

State doc: ``users/{uid}/presence/state``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from google.cloud import firestore as fs  # type: ignore

from ...lib.logger import logger
from ..firebase import admin_firestore

PRESENCE_SUBCOLLECTION = "presence"
PRESENCE_DOC = "state"

FIELD_LAST_APP_OPEN = "last_app_open"
FIELD_DISMISS_STREAK = "dismiss_streak"
FIELD_LAST_TAP_AT = "last_tap_at"
FIELD_LAST_DISMISS_AT = "last_dismiss_at"

# How long after an app open we treat the user as "still in the app." A few minutes:
# long enough to cover the start of a session, short enough that we are not silenced
# for a user who opened once hours ago.
FOREGROUND_WINDOW = timedelta(minutes=3)

# Consecutive dismissals (no tap/open between) before Buddy goes quiet.
DISMISS_BACKOFF_THRESHOLD = 3


def _ref(uid: str):
    return (
        admin_firestore()
        .collection("users")
        .document(uid)
        .collection(PRESENCE_SUBCOLLECTION)
        .document(PRESENCE_DOC)
    )


def _as_aware(value) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    return None


# ── Writers (from the behavioral bridge) ─────────────────────────────────────
async def record_app_open(uid: str, *, now: datetime | None = None) -> None:
    """App open: mark foreground and clear the dismiss streak (an open is the
    strongest 'I'm engaged' signal there is)."""
    when = now or datetime.now(UTC)
    await _write(uid, {FIELD_LAST_APP_OPEN: when, FIELD_DISMISS_STREAK: 0})


async def record_tap(uid: str, *, now: datetime | None = None) -> None:
    """Notification tap: a positive signal, so clear the dismiss streak."""
    when = now or datetime.now(UTC)
    await _write(uid, {FIELD_LAST_TAP_AT: when, FIELD_DISMISS_STREAK: 0})


async def record_dismiss(uid: str, *, now: datetime | None = None) -> None:
    """Notification dismissed: bump the streak (atomic Increment)."""
    when = now or datetime.now(UTC)
    await _write(uid, {
        FIELD_LAST_DISMISS_AT: when,
        FIELD_DISMISS_STREAK: fs.Increment(1),
    })


async def _write(uid: str, fields: dict) -> None:
    def _set() -> None:
        _ref(uid).set(fields, merge=True)

    try:
        await asyncio.to_thread(_set)
    except Exception as exc:
        logger.warn("presence._write failed", {"user_id": uid, "error": str(exc)})


# ── Reader (the surface-aware gate) ──────────────────────────────────────────
async def should_hold_proactive(uid: str, *, now: datetime | None = None) -> tuple[bool, str]:
    """True (+ reason) if the proactive lane should hold for this user right now:
    they are foreground, or on a dismiss streak. Fails OPEN (``False``) on any read
    error — presence must never silence a notification by accident."""
    when = now or datetime.now(UTC)

    def _read() -> dict:
        snap = _ref(uid).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    try:
        data = await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn("presence.should_hold_proactive read failed (fail-open)", {
            "user_id": uid, "error": str(exc),
        })
        return False, ""

    last_open = _as_aware(data.get(FIELD_LAST_APP_OPEN))
    if last_open is not None and (when - last_open) <= FOREGROUND_WINDOW:
        return True, "foreground"

    streak = int(data.get(FIELD_DISMISS_STREAK, 0) or 0)
    if streak >= DISMISS_BACKOFF_THRESHOLD:
        return True, "dismiss_backoff"

    return False, ""
