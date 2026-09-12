"""GET /home/status — everything the home screen's status pills need, in one call.

Four separate calls on app open would be four round-trips for one strip of text,
so this gathers them in parallel and answers once. Nothing here is allowed to be
expensive: no model call, no full briefing payload, and no query that needs an
index the project does not already have.

Every section is isolated. A failure in one of them yields that field empty
rather than a 500, because a missing pill is a smaller problem than a home screen
that cannot finish loading.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services import rituals
from ..services.briefing import fields as bf
from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_request
from ..services.tracking import fields as tf
from ..services.tracking import tracking_store

# Enough to cover any realistic day for one user without an unbounded scan. The
# query is single-field (status ==), which Firestore auto-indexes, so this stays
# cheap; the day filter happens in memory for the same reason the alarm-sync path
# does it that way (handlers/reminders.py) — a composite index on a path the home
# screen depends on is one deployment away from silently returning nothing.
_PENDING_REMINDER_SCAN_LIMIT = 200

# Pills are a glance, not a list.
_MAX_RITUAL_PILLS = 3
_MAX_TRACKER_PILLS = 3


def _user_timezone(user_id: str) -> str:
    """One read of users/{uid}, reused for both the briefing date and the day window."""
    try:
        snap = admin_firestore().collection("users").document(user_id).get()
        if snap.exists:
            value = (snap.to_dict() or {}).get("timezone")
            if isinstance(value, str) and value.strip():
                return value.strip()
    except Exception as exc:
        logger.warn("home_status: timezone read failed, defaulting UTC", {
            "user_id": user_id, "error": str(exc),
        })
    return "UTC"


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, ValueError):
        return datetime.now(UTC)


def _briefing_ready(user_id: str, local_date: str) -> bool:
    """Is today's briefing already generated?

    Deliberately NOT the /briefing/today path: that returns the whole narrative
    and every item, and the client's briefing viewmodel generates one on a miss.
    This reads a single document and only its status field.
    """
    try:
        snap = (
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection(bf.DAILY_BRIEFING_SUBCOLLECTION)
            .document(local_date)
            .get(field_paths=[bf.FIELD_STATUS])
        )
        if not snap.exists:
            return False
        return str((snap.to_dict() or {}).get(bf.FIELD_STATUS, "")) == bf.STATUS_READY
    except Exception as exc:
        logger.warn("home_status: briefing readiness read failed", {
            "user_id": user_id, "error": str(exc),
        })
        return False


def _reminders_today(user_id: str, local_now: datetime) -> int:
    """How many pending reminders land on the user's local calendar day."""
    try:
        docs = list(
            admin_firestore()
            .collection("users")
            .document(user_id)
            .collection("reminders")
            .where("status", "==", "pending")
            .limit(_PENDING_REMINDER_SCAN_LIMIT)
            .stream()
        )
    except Exception as exc:
        logger.warn("home_status: reminder scan failed", {
            "user_id": user_id, "error": str(exc),
        })
        return 0

    today = local_now.date()
    zone = local_now.tzinfo or UTC
    count = 0
    for doc in docs:
        raw = (doc.to_dict() or {}).get("trigger_at")
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            when = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        when = when if when.tzinfo else when.replace(tzinfo=UTC)
        if when.astimezone(zone).date() == today:
            count += 1
    return count


async def _ritual_pills(user_id: str) -> dict[str, Any]:
    """The pills, plus the counts behind them.

    The pill list is capped at three, so it can never tell the client whether the
    user is at the ritual limit. The counts are returned alongside it precisely so
    the home deck can stop offering a ritual it knows the server would refuse.
    """
    items = await rituals.list_rituals(user_id)
    active = [item for item in items if str(item.get("status")) == rituals.STATUS_ACTIVE]
    active.sort(key=lambda item: str(item.get("next_fire_at") or ""))
    return {
        "pills": [
            {
                "ritual_id": str(item.get("id", "")),
                "title": str(item.get("title", "")),
                "next_fire_local": str(item.get("next_fire_local", "")),
            }
            for item in active[:_MAX_RITUAL_PILLS]
        ],
        "active": len(active),
        "max": rituals.MAX_ACTIVE_RITUALS,
    }


async def _tracker_pills(user_id: str) -> list[dict[str, Any]]:
    trackers = await tracking_store.list_trackers_for_user(user_id)
    active = [t for t in trackers if t.status == tf.TRACKER_STATUS_ACTIVE]
    pills: list[dict[str, Any]] = []
    for tracker in active[:_MAX_TRACKER_PILLS]:
        topic = await tracking_store.get_tracked_topic(tracker.topic_key)
        title = (topic.title if topic else "") or tracker.topic_key.replace("-", " ")
        pills.append({"tracker_id": tracker.id, "title": title})
    return pills


async def handle_home_status(request: Request) -> JSONResponse:
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    timezone_name = await asyncio.to_thread(_user_timezone, user_id)
    local_now = _local_now(timezone_name)

    briefing, reminders, ritual_state, tracker_pills = await asyncio.gather(
        asyncio.to_thread(_briefing_ready, user_id, local_now.date().isoformat()),
        asyncio.to_thread(_reminders_today, user_id, local_now),
        _ritual_pills(user_id),
        _tracker_pills(user_id),
        return_exceptions=True,
    )

    def _or_default(value: Any, default: Any, label: str) -> Any:
        if isinstance(value, BaseException):
            logger.warn("home_status: section failed", {
                "user_id": user_id, "section": label, "error": str(value),
            })
            return default
        return value

    ritual_state = _or_default(
        ritual_state,
        {"pills": [], "active": 0, "max": rituals.MAX_ACTIVE_RITUALS},
        "rituals",
    )

    return JSONResponse({
        "briefing_ready": bool(_or_default(briefing, False, "briefing")),
        "reminders_today": int(_or_default(reminders, 0, "reminders")),
        "rituals": ritual_state["pills"],
        "trackers": _or_default(tracker_pills, [], "trackers"),
        # A failed section reports 0 active, which the client reads as "unknown"
        # and keeps offering rituals. Letting a read failure look like being at
        # the cap would silently disable the card.
        "rituals_active": int(ritual_state["active"]),
        "rituals_max": int(ritual_state["max"]),
    })
