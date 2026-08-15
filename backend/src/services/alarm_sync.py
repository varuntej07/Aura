"""Silent alarm-schedule distribution to the user's mobile devices.

An alarm is not a notification. FCM cannot wake a doze'd phone at 3 AM, and no
amount of priority on a push changes that: the OS decides when a background app
gets to make noise. The only mechanism that reliably rings is a LOCAL schedule
registered with the platform ahead of time (``AlarmManager.setAlarmClock`` on
Android, ``UNNotificationRequest`` on iOS), which means the device has to learn
about the alarm BEFORE it is due.

So this module carries schedules, not alerts. Every message here is data-only and
produces no user-visible artifact on any platform; the device acts on it and the
user never sees that it arrived.

WHY THIS DOES NOT GO THROUGH THE NOTIFICATION FUNNEL
----------------------------------------------------
The repo rule is that all notification INTENT flows through ``NotificationProposal``
-> ``orchestrator.submit()``. That funnel exists to arbitrate things the user will
see: quiet hours, budget caps, presence gating, priority ladders. A schedule sync
is none of those. Routing it through the orchestrator would put quiet hours in
front of the very mechanism whose entire job is to fire during quiet hours, and
would spend a notification budget slot on a message nobody sees.

The user-visible side of an alarm is unchanged and still goes through the funnel:
``handlers/scheduler.py`` submits a ``SOURCE_REMINDER`` proposal at fire time as a
backstop for a device that never scheduled locally.

DELIVERY IS BEST-EFFORT BY DESIGN
---------------------------------
A dropped push must never mean a missed alarm, so this is only the fast path.
``GET /reminders/alarms`` is the slow, authoritative one: the client reconciles
its full schedule from it on app start, on resume, and after a reboot. Anything
lost here is repaired there.
"""

from __future__ import annotations

import asyncio
from typing import Any

from firebase_admin import messaging

from ..lib.logger import logger
from . import alarm_tones, alarm_voice
from .fcm_token_registry import (
    get_user_tokens,
    invalid_token_reason,
    remove_invalid_tokens,
)
from .firebase import admin_messaging

# The two reminder tiers. An absent field means "reminder": every document
# written before this feature existed is a plain reminder, and already-released
# app builds ignore the field entirely.
TIER_REMINDER = "reminder"
TIER_ALARM = "alarm"
VALID_TIERS = frozenset({TIER_REMINDER, TIER_ALARM})

# Client routing key. Deliberately NOT "reminder": an old app build that has never
# heard of alarms must not render this control message as a notification, and its
# tap-dispatch switch only matches known types.
NOTIFICATION_TYPE = "alarm_sync"

OP_SCHEDULE = "schedule"
OP_CANCEL = "cancel"
OP_STOP = "stop"

# Web has no background alarm capability at all, so sending it control traffic
# it cannot act on is pure noise. This is the one send path in the backend that
# reads the stored `platform` field rather than blasting every token.
#
# Stated as an EXCLUSION, not an allowlist: a token document written before
# `platform` was recorded, or by a client reporting a value we have not seen,
# is far more likely to be a phone than a browser. The cost of guessing wrong in
# the excluding direction is a silently missed alarm, which is the exact bug
# this feature exists to fix.
_NON_SCHEDULING_PLATFORMS = frozenset({"web"})

# A schedule is worthless once its alarm time has passed, and a stale one
# arriving days later would re-arm an alarm the user already dealt with. Four
# hours is long enough to cover a phone that is off overnight before an evening
# alarm, and short enough that FCM's four-WEEK default can never resurrect one.
_TTL_SECONDS = 4 * 60 * 60


def normalize_tier(value: Any) -> str:
    """Coerce any stored or model-supplied tier to a known one, defaulting quiet.

    Anything unrecognised becomes a plain reminder. An unknown value must never
    fall through to the loud tier: a wrong 3 AM ring costs far more than a
    missed banner.
    """
    tier = str(value or "").strip().lower()
    return tier if tier in VALID_TIERS else TIER_REMINDER


def is_alarm(reminder: dict[str, Any]) -> bool:
    return normalize_tier(reminder.get("tier")) == TIER_ALARM


async def push_schedule(user_id: str, reminder: dict[str, Any]) -> int:
    """Tell every schedule-capable device to arm this alarm locally.

    ``reminder`` is the Firestore document. Carries the wall-clock fields as well
    as the UTC instant because an alarm is a wall-clock promise: "wake me at 6"
    means 6 AM where the sleeper actually is, so the device re-resolves it
    against its own current timezone.

    ``tone`` is resolved here rather than on the device: the precedence between a
    per-alarm override and the user's default lives in one place, and the phone
    receives one concrete slug it can simply play.
    """
    tone = alarm_tones.resolve_tone(
        reminder.get("tone"),
        await alarm_tones.user_default_tone(user_id),
    )
    clip_tag = ""
    if alarm_tones.speaks_aloud(tone):
        # Only computed for the one tone that speaks. It is a hash, but the
        # inputs behind it cost a Firestore read and an entitlement resolve, and
        # the other seven tones have no use for the answer.
        clip_tag = alarm_voice.clip_tag(
            str(reminder.get("message", ""))[:200],
            await alarm_voice.resolved_voice_slug(user_id),
        )
    return await _push(user_id, {
        "op": OP_SCHEDULE,
        "reminder_id": str(reminder.get("id", "")),
        "message": str(reminder.get("message", ""))[:200],
        "trigger_at": str(reminder.get("trigger_at", "")),
        "local_time": str(reminder.get("local_time", "")),
        "timezone": str(reminder.get("timezone", "")),
        "tone": tone,
        "clip_tag": clip_tag,
    })


async def push_cancel(user_id: str, reminder_id: str) -> int:
    """Disarm a local alarm the user cancelled or rescheduled elsewhere."""
    return await _push(user_id, {"op": OP_CANCEL, "reminder_id": reminder_id})


async def push_stop(user_id: str, reminder_id: str, *, action: str) -> int:
    """Silence an alarm that is ringing right now on the user's other devices.

    The phone and the tablet both hold the same local schedule, so both ring.
    Whichever one the user actually reaches for acks first, and this stops the
    others. Best-effort: a device that misses this keeps ringing until it is
    dismissed by hand, which is annoying but never dangerous.
    """
    return await _push(user_id, {
        "op": OP_STOP,
        "reminder_id": reminder_id,
        "action": action,
    })


async def _push(user_id: str, payload: dict[str, str]) -> int:
    """Send one silent control message. Never raises: this is the fast path only.

    Returns the number of tokens that accepted it, for logging. A zero here is
    not an error, a user with only a web session has nothing to schedule on.
    """
    try:
        token_docs: list[dict[str, Any]] = await asyncio.to_thread(get_user_tokens, user_id)
    except Exception as exc:
        logger.warn("alarm_sync: token fetch failed; reconcile will repair", {
            "user_id": user_id,
            "op": payload.get("op"),
            "error": str(exc),
        })
        return 0

    tokens = [
        str(doc["token"])
        for doc in token_docs
        if str(doc.get("platform", "")).lower() not in _NON_SCHEDULING_PLATFORMS
    ]
    if not tokens:
        logger.info("alarm_sync: no schedule-capable devices", {
            "user_id": user_id,
            "op": payload.get("op"),
            "tokens_total": len(token_docs),
        })
        return 0

    data = {"notification_type": NOTIFICATION_TYPE, "user_id": user_id, **payload}

    message = messaging.MulticastMessage(
        tokens=tokens,
        # No `notification` block and no APNS `alert`. This is the difference
        # between this path and send_notification(data_only=True), which still
        # renders an iOS banner from aps.alert, here there is nothing to show.
        data=data,
        android=messaging.AndroidConfig(priority="high", ttl=_TTL_SECONDS),
        apns=messaging.APNSConfig(
            headers={
                # Silent background push. apns-priority 5 is REQUIRED for a
                # content-available-only payload; Apple throttles or rejects
                # priority 10 without an alert.
                "apns-priority": "5",
                "apns-push-type": "background",
            },
            payload=messaging.APNSPayload(aps=messaging.Aps(content_available=True)),
        ),
    )

    try:
        response = await asyncio.to_thread(
            admin_messaging().send_each_for_multicast, message
        )
    except Exception as exc:
        logger.warn("alarm_sync: FCM send failed; reconcile will repair", {
            "user_id": user_id,
            "op": payload.get("op"),
            "reminder_id": payload.get("reminder_id"),
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return 0

    # Prune tokens FCM says are permanently dead, grouped by the reason it gave
    # so the removal audit stays honest about WHY each device was dropped.
    #
    # payload_validated stays False (the default). This payload is small and
    # fixed, so an "invalid argument" here is almost certainly the token, but a
    # wrongly-pruned token means the user silently stops receiving alarm
    # schedules altogether. Only unambiguous codes are worth acting on.
    invalid_by_reason: dict[str, list[str]] = {}
    for idx, result in enumerate(response.responses):
        if result.success:
            continue
        reason = invalid_token_reason(result.exception)
        if reason:
            invalid_by_reason.setdefault(reason, []).append(tokens[idx])

    invalid_count = sum(len(group) for group in invalid_by_reason.values())
    for reason, group in invalid_by_reason.items():
        await asyncio.to_thread(
            remove_invalid_tokens, user_id, group, reason=f"alarm_sync:{reason}"
        )

    logger.info("alarm_sync: control message sent", {
        "user_id": user_id,
        "op": payload.get("op"),
        "reminder_id": payload.get("reminder_id"),
        "tokens_targeted": len(tokens),
        "success_count": response.success_count,
        "failure_count": response.failure_count,
        "invalid_removed": invalid_count,
    })
    return response.success_count
