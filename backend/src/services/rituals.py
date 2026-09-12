"""Recurring rituals: the things Buddy does for you every day without being asked.

A ritual is deliberately NOT a new delivery subsystem. The series lives in
``users/{uid}/rituals/{ritual_id}``, but each occurrence is an ordinary document
in ``users/{uid}/reminders/{id}``, so it inherits the whole reminder pipeline that
is already deployed and already correct: the (status, trigger_at) collection-group
index, the per-minute due scan, the atomic claim, the stuck-processing requeue,
the retry and expiry policy, the /reminders screen, and SOURCE_REMINDER's place in
the committed notification lane. A ritual the user set for 8:00 therefore arrives
at 8:00 instead of waiting behind the proactive budget.

The occurrence id is deterministic (``{ritual_id}_{local_date}``). That is the
structural guarantee against double-fire: a re-arm that runs twice addresses the
same document, so the second write is a no-op rather than a second notification.

Content is generated when the NEXT occurrence is armed, not when it fires, so the
8:00 push does no model work and cannot be broken by a provider being down at 8:00.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

from google.cloud.firestore_v1.base_query import FieldFilter

from ..lib.logger import logger
from ..prompts import RITUAL_BODY_SYSTEM_PROMPT, ritual_body_user_prompt
from .firebase import admin_firestore
from .model_provider import get_model_provider
from .reminder_time import next_ritual_fire_at

# The content kinds a ritual card may offer. Closed set: the deck generator picks
# one of these through a Literal, and anything else never reaches storage.
CONTENT_KIND_JOKE = "joke"
CONTENT_KIND_MOTIVATION = "motivation"
CONTENT_KIND_CHECKIN = "checkin"
CONTENT_KIND_QUESTION = "question"
VALID_CONTENT_KINDS = frozenset({
    CONTENT_KIND_JOKE,
    CONTENT_KIND_MOTIVATION,
    CONTENT_KIND_CHECKIN,
    CONTENT_KIND_QUESTION,
})

STATUS_ACTIVE = "active"
STATUS_PAUSED = "paused"
STATUS_DELETED = "deleted"
VALID_STATUSES = frozenset({STATUS_ACTIVE, STATUS_PAUSED, STATUS_DELETED})

# Every ritual is a recurring push and a daily model call, so the cap is both a
# cost bound and a politeness bound: past a handful, Buddy is spamming.
MAX_ACTIVE_RITUALS = 5

# How many past bodies to keep so the generator does not repeat yesterday's joke.
_RECENT_BODY_MEMORY = 5

# A ritual is a promise about a wall-clock moment, so a missed one is worth far
# less an hour later. Occurrences inherit the reminder delivery grace.
_UNARMED_GRACE = timedelta(minutes=10)

class RitualLimitReached(Exception):
    """Raised when a user already holds the maximum number of active rituals."""


_STATIC_BODIES = {
    CONTENT_KIND_JOKE: "I had a joke ready and lost it. Ask me for it and I'll find another.",
    CONTENT_KIND_MOTIVATION: "One small thing today, done properly. That's the whole trick.",
    CONTENT_KIND_CHECKIN: "How's your day going so far?",
    CONTENT_KIND_QUESTION: "What's on your mind right now?",
}

_SYSTEM_PROMPT = " ".join(RITUAL_BODY_SYSTEM_PROMPT.split())


def _rituals_ref(user_id: str):
    return admin_firestore().collection("users").document(user_id).collection("rituals")


def _reminders_ref(user_id: str):
    return admin_firestore().collection("users").document(user_id).collection("reminders")


def occurrence_id(ritual_id: str, local_date: str) -> str:
    """Deterministic id for one occurrence. Re-arming twice is a no-op, not a double send."""
    return f"{ritual_id}_{local_date}"


def normalize_content_kind(value: Any) -> str:
    kind = str(value or "").strip().lower()
    return kind if kind in VALID_CONTENT_KINDS else CONTENT_KIND_CHECKIN


async def generate_ritual_body(
    *,
    content_kind: str,
    title: str,
    local_day_label: str,
    recent_bodies: list[str],
) -> tuple[str, bool]:
    """Write the text one occurrence will deliver. Returns (body, used_fallback).

    Never raises. A ritual that stops arriving because a model call failed is
    indistinguishable to the user from the feature being broken, so a failure here
    falls back to a static line and the occurrence is still armed and still fires.
    """
    kind = normalize_content_kind(content_kind)
    try:
        provider = get_model_provider()
        raw = await provider.cheap(
            ritual_body_user_prompt(
                content_kind=kind,
                title=title,
                local_day_label=local_day_label,
                recent_bodies=recent_bodies,
            ),
            system=_SYSTEM_PROMPT,
            # High enough that a daily joke does not converge on one voice, and the
            # RECENT list in the prompt carries the actual anti-repetition weight.
            temperature=0.9,
            max_output_tokens=220,
            attempt_timeout_s=20,
        )
        body = " ".join(str(raw).strip().split())
        if body:
            return body[:400], False
    except Exception as exc:
        logger.warn("Ritual body generation failed, using static copy", {
            "content_kind": kind,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
    return _STATIC_BODIES[kind], True


_DAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _schedule_summary(ritual: dict[str, Any], local: datetime) -> str:
    """One line describing the series, for the reminders screen."""
    clock = local.strftime("%I:%M %p").lstrip("0").lower()
    if str(ritual.get("freq")) == "weekly":
        days = sorted({int(day) for day in (ritual.get("weekdays") or [])})
        if days == [0, 1, 2, 3, 4]:
            label = "weekdays"
        elif days == [5, 6]:
            label = "weekends"
        else:
            label = ", ".join(_DAY_NAMES[day] for day in days if 0 <= day <= 6)
        return f"Repeats {label} at {clock}"
    return f"Repeats daily at {clock}"


async def _user_timezone(user_id: str) -> str:
    """The IANA zone the client wrote at sign-in. Never taken from a request body."""
    def _fetch() -> str | None:
        snap = admin_firestore().collection("users").document(user_id).get()
        value = (snap.to_dict() or {}).get("timezone")
        return value.strip() if isinstance(value, str) else None

    timezone_name = await asyncio.to_thread(_fetch)
    if not timezone_name:
        raise ValueError(
            "I need your current timezone before I can set this safely. "
            "Refresh your device timezone, then try again."
        )
    return timezone_name


async def arm_next_occurrence(
    user_id: str,
    ritual: dict[str, Any],
    *,
    after: datetime | None = None,
) -> dict[str, Any] | None:
    """Compute, generate and write the ritual's next occurrence.

    Returns the occurrence document, or None when the ritual is not active or its
    schedule cannot be resolved. Safe to call twice for the same instant: the
    deterministic occurrence id makes the second call an idempotent overwrite, and
    an occurrence that has already left "pending" is never resurrected.
    """
    ritual_id = str(ritual.get("id") or "")
    if not ritual_id or str(ritual.get("status")) != STATUS_ACTIVE:
        return None

    # Re-read the zone rather than trusting the one stored at creation: someone who
    # moved should get their 8:00 ritual at 8:00 where they now are, from the next
    # occurrence onward.
    try:
        timezone_name = await _user_timezone(user_id)
    except ValueError:
        timezone_name = str(ritual.get("timezone") or "")
        if not timezone_name:
            logger.warn("Ritual has no usable timezone, not arming", {
                "user_id": user_id, "ritual_id": ritual_id,
            })
            return None

    try:
        fire = next_ritual_fire_at(
            freq=str(ritual.get("freq") or "daily"),
            hour=int(ritual.get("hour") or 0),
            minute=int(ritual.get("minute") or 0),
            weekdays=[int(day) for day in (ritual.get("weekdays") or [])],
            timezone_name=timezone_name,
            after=after,
        )
    except (ValueError, TypeError) as exc:
        logger.error("Ritual schedule could not be resolved, not arming", {
            "user_id": user_id, "ritual_id": ritual_id, "error": str(exc), "alert": True,
        })
        return None

    local_date = fire.local.date().isoformat()
    doc_id = occurrence_id(ritual_id, local_date)
    occurrence_ref = _reminders_ref(user_id).document(doc_id)

    existing = await asyncio.to_thread(occurrence_ref.get)
    if existing.exists:
        current = existing.to_dict() or {}
        if str(current.get("status")) != "pending":
            # Already fired, dismissed or expired. Arming the day after it instead
            # is what keeps a recovery sweep from resurrecting a delivered push.
            return await arm_next_occurrence(
                user_id,
                ritual,
                after=fire.utc + timedelta(seconds=1),
            )

    title = str(ritual.get("title") or "Buddy")
    body, used_fallback = await generate_ritual_body(
        content_kind=str(ritual.get("content_kind") or CONTENT_KIND_CHECKIN),
        title=title,
        local_day_label=fire.local.strftime("%A %B %d at %I:%M %p").replace(" 0", " "),
        recent_bodies=[str(item) for item in (ritual.get("recent_bodies") or [])],
    )

    now_iso = datetime.now(UTC).isoformat()
    occurrence = {
        "id": doc_id,
        # The generated line IS the notification. The rewriter would cost a second
        # model call and can move half a punchline into the title.
        "message": body,
        "trigger_at": fire.utc.isoformat(),
        "status": "pending",
        # Never the alarm tier: a ritual is a warm nudge, and arming a device alarm
        # for a daily joke would be a hostile way to learn about this feature.
        "tier": "reminder",
        "local_time": fire.local.replace(tzinfo=None).isoformat(),
        "timezone": fire.timezone,
        "created_via": "ritual",
        "snooze_count": 0,
        "created_at": now_iso,
        "ritual_id": ritual_id,
        "ritual_content_kind": normalize_content_kind(ritual.get("content_kind")),
        "ritual_title": title,
        # Read by the reminders screen so an occurrence says it is part of a
        # series, and offers to stop the series rather than just this one.
        "ritual_summary": _schedule_summary(ritual, fire.local),
        "skip_copy_rewrite": True,
    }
    await asyncio.to_thread(occurrence_ref.set, occurrence)

    recent = [body, *[str(item) for item in (ritual.get("recent_bodies") or [])]]
    await asyncio.to_thread(
        _rituals_ref(user_id).document(ritual_id).set,
        {
            "next_fire_at": occurrence["trigger_at"],
            "next_fire_local": occurrence["local_time"],
            "armed_occurrence_id": doc_id,
            "timezone": fire.timezone,
            "recent_bodies": recent[:_RECENT_BODY_MEMORY],
            "updated_at": now_iso,
        },
        True,
    )

    logger.info("Ritual occurrence armed", {
        "user_id": user_id,
        "ritual_id": ritual_id,
        "occurrence_id": doc_id,
        "trigger_at": occurrence["trigger_at"],
        "static_body": used_fallback,
    })
    return occurrence


async def create_ritual(
    user_id: str,
    *,
    ritual_id: str,
    content_kind: str,
    title: str,
    freq: str,
    hour: int,
    minute: int,
    weekdays: list[int],
) -> dict[str, Any]:
    """Create a ritual and arm its first occurrence.

    ``ritual_id`` is supplied by the client and becomes the document id, so a
    double-tapped card writes the same document twice instead of creating two
    rituals. Returns the stored document plus ``created``.
    """
    ref = _rituals_ref(user_id).document(ritual_id)
    existing = await asyncio.to_thread(ref.get)
    if existing.exists:
        stored = existing.to_dict() or {}
        if str(stored.get("status")) != STATUS_DELETED:
            return {**stored, "created": False}

    active = await asyncio.to_thread(
        lambda: list(
            _rituals_ref(user_id)
            .where(filter=FieldFilter("status", "==", STATUS_ACTIVE))
            .limit(MAX_ACTIVE_RITUALS + 1)
            .stream()
        )
    )
    if len(active) >= MAX_ACTIVE_RITUALS:
        raise RitualLimitReached(
            "That's as many rituals as I can keep for you. Stop one first."
        )

    # Validate the schedule before anything is written, so an impossible one fails
    # at the tap rather than silently never firing.
    timezone_name = await _user_timezone(user_id)
    next_ritual_fire_at(
        freq=freq,
        hour=hour,
        minute=minute,
        weekdays=weekdays,
        timezone_name=timezone_name,
    )

    now_iso = datetime.now(UTC).isoformat()
    document = {
        "id": ritual_id,
        "content_kind": normalize_content_kind(content_kind),
        "title": " ".join(str(title).strip().split())[:120] or "A moment with Buddy",
        "freq": str(freq).strip().lower(),
        "hour": int(hour),
        "minute": int(minute),
        "weekdays": sorted({int(day) for day in weekdays}),
        "timezone": timezone_name,
        "status": STATUS_ACTIVE,
        "fire_count": 0,
        "created_via": "deck",
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    await asyncio.to_thread(ref.set, document)
    await arm_next_occurrence(user_id, document)

    stored = await asyncio.to_thread(ref.get)
    return {**(stored.to_dict() or document), "created": True}


async def set_ritual_status(user_id: str, ritual_id: str, status: str) -> dict[str, Any] | None:
    """Pause, resume or delete a ritual, cancelling or re-arming its occurrence.

    Stopping has to actually stop: the armed occurrence is cancelled in the same
    call, because leaving it pending would deliver one more push after the user
    told Buddy to stop.
    """
    if status not in VALID_STATUSES:
        raise ValueError("A ritual is either active, paused, or deleted.")

    ref = _rituals_ref(user_id).document(ritual_id)
    snap = await asyncio.to_thread(ref.get)
    if not snap.exists:
        return None
    ritual = snap.to_dict() or {}

    armed_id = str(ritual.get("armed_occurrence_id") or "")
    if status in {STATUS_PAUSED, STATUS_DELETED} and armed_id:
        await asyncio.to_thread(_cancel_occurrence, user_id, armed_id)

    now_iso = datetime.now(UTC).isoformat()
    update: dict[str, Any] = {"status": status, "updated_at": now_iso}
    if status in {STATUS_PAUSED, STATUS_DELETED}:
        update["armed_occurrence_id"] = ""
        update["next_fire_at"] = ""
    await asyncio.to_thread(ref.set, update, True)

    if status == STATUS_ACTIVE:
        await arm_next_occurrence(user_id, {**ritual, **update})
        refreshed = await asyncio.to_thread(ref.get)
        return refreshed.to_dict() or {**ritual, **update}
    return {**ritual, **update}


def _cancel_occurrence(user_id: str, reminder_id: str) -> None:
    """Take one armed occurrence out of the scheduler's view. Synchronous by design."""
    ref = _reminders_ref(user_id).document(reminder_id)
    snap = ref.get()
    if not snap.exists:
        return
    if str((snap.to_dict() or {}).get("status")) != "pending":
        return
    ref.update({
        "status": "dismissed",
        "dismissed_at": datetime.now(UTC).isoformat(),
        "delivery_terminal_reason": "ritual_stopped",
    })


async def list_rituals(user_id: str) -> list[dict[str, Any]]:
    docs = await asyncio.to_thread(
        lambda: list(
            _rituals_ref(user_id)
            .where(filter=FieldFilter("status", "in", [STATUS_ACTIVE, STATUS_PAUSED]))
            .limit(MAX_ACTIVE_RITUALS * 2)
            .stream()
        )
    )
    return [doc.to_dict() or {} for doc in docs]


async def on_occurrence_fired(user_id: str, occurrence: dict[str, Any]) -> None:
    """Re-arm the series after one occurrence was actually delivered.

    Called from the delivery path only once the transport accepted the push and the
    occurrence was marked fired, never before: arming tomorrow's joke for a push
    that never went out is how a series silently drifts a day ahead of itself.
    Never raises — a failure here is repaired by the hourly recovery sweep.
    """
    ritual_id = str(occurrence.get("ritual_id") or "")
    if not ritual_id:
        return
    try:
        ref = _rituals_ref(user_id).document(ritual_id)
        snap = await asyncio.to_thread(ref.get)
        if not snap.exists:
            return
        ritual = snap.to_dict() or {}
        if str(ritual.get("status")) != STATUS_ACTIVE:
            return
        await asyncio.to_thread(
            ref.set,
            {
                "last_fired_at": datetime.now(UTC).isoformat(),
                "fire_count": int(ritual.get("fire_count") or 0) + 1,
            },
            True,
        )
        await arm_next_occurrence(user_id, ritual)
    except Exception as exc:
        logger.error("Failed to re-arm ritual after fire", {
            "user_id": user_id,
            "ritual_id": ritual_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
            "alert": True,
        })


def fetch_unarmed_rituals(limit: int = 200) -> list[dict[str, Any]]:
    """Active rituals whose armed instant has passed without a re-arm.

    The normal re-arm happens inline after delivery; this catches the cases that
    path cannot: an instance killed between the send and the re-arm, or an arming
    that threw. Synchronous by design (called via asyncio.to_thread) and never
    raises, so a missing index degrades recovery instead of taking down the tick.
    """
    cutoff = (datetime.now(UTC) - _UNARMED_GRACE).isoformat()
    try:
        docs = list(
            admin_firestore()
            .collection_group("rituals")
            .where(filter=FieldFilter("status", "==", STATUS_ACTIVE))
            .where(filter=FieldFilter("next_fire_at", "<=", cutoff))
            .order_by("next_fire_at")
            .limit(limit)
            .stream()
        )
    except Exception as exc:
        logger.error(
            "fetch_unarmed_rituals FAILED — no rituals recovered this sweep "
            "(check the rituals (status, next_fire_at) COLLECTION_GROUP index)",
            {"error": str(exc), "error_type": type(exc).__name__, "alert": True},
        )
        return []

    results: list[dict[str, Any]] = []
    for doc in docs:
        parent = doc.reference.parent.parent
        if parent is None:
            logger.error("Could not resolve userId for ritual", {"doc_id": doc.id})
            continue
        results.append({"userId": parent.id, "data": doc.to_dict() or {}})
    return results


async def sweep_unarmed_rituals() -> int:
    """Re-arm every ritual that lost its occurrence. Returns how many were repaired."""
    rows = await asyncio.to_thread(fetch_unarmed_rituals)
    if not rows:
        return 0

    repaired = 0
    for row in rows:
        try:
            occurrence = await arm_next_occurrence(row["userId"], row["data"])
            repaired += 1 if occurrence else 0
        except Exception as exc:
            logger.error("Ritual recovery arming failed", {
                "user_id": row.get("userId"),
                "ritual_id": (row.get("data") or {}).get("id"),
                "error": str(exc),
            })

    logger.warn("Rituals recovered by sweep", {
        "found": len(rows), "repaired": repaired, "alert": repaired > 0,
    })
    return repaired
