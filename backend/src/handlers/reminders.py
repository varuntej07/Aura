"""Alarm schedule sync and acknowledgement.

  GET  /reminders/alarms       -> every armed alarm, so a device can reconcile
  POST /reminders/{id}/ack     -> dismiss / snooze / "I'm up" from a ringing alarm

WHY A SYNC ENDPOINT EXISTS AT ALL
---------------------------------
An alarm rings from a schedule the OS holds locally, which means the device has
to be told about it in advance. ``services/alarm_sync.py`` pushes that schedule
the moment an alarm is created, but a push is best-effort: it is dropped while
the phone is off, it is not replayed after a reboot wipes the alarm table, and a
device that signed in yesterday never received one at all.

So the push is the fast path and this is the authoritative one. The client calls
GET /reminders/alarms on app start, on resume, after BOOT_COMPLETED, and after a
package replace, then reconciles: arm anything missing, disarm anything gone.
Every failure mode of the push path collapses into "the next reconcile fixes it."

WHY THE CLIENT DOES NOT JUST WRITE FIRESTORE DIRECTLY
-----------------------------------------------------
The reminders collection is owner-writable and the Flutter app already reads it
directly, so an ack COULD be a client-side write. It goes through here because a
single ack has to do three things atomically enough to be trusted: settle the
document, re-arm on snooze, and silence the user's OTHER devices, which are
ringing at the same moment and cannot be reached from the client at all.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Request, Response
from fastapi.responses import JSONResponse

from ..lib.logger import logger
from ..services import alarm_sync, alarm_tones, alarm_voice
from ..services.feedback.feedback_capture import capture_feedback
from ..services.feedback.feedback_schema import FeedbackReport
from ..services.firebase import admin_firestore
from ..services.request_auth import resolve_user_id_from_request

# How far ahead a device arms alarms. Android's alarm table is a finite shared
# resource and a schedule that far out is re-checked many times before it fires,
# so there is nothing to gain from a longer horizon.
SYNC_HORIZON_DAYS = 7

# The snooze the UI offers is 9 minutes (the interval every physical alarm clock
# has used since mechanical ones), but the client sends it explicitly so the
# duration is never silently disagreed about across surfaces.
DEFAULT_SNOOZE_MINUTES = 9
MAX_SNOOZE_MINUTES = 120

ACTION_DISMISS = "dismiss"
ACTION_SNOOZE = "snooze"
ACTION_IM_UP = "im_up"
VALID_ACTIONS = frozenset({ACTION_DISMISS, ACTION_SNOOZE, ACTION_IM_UP})

_ALARM_INTEREST_LABELS = {
    "sunrise_alarm": "Sunrise Alarm",
    "weather_forecast": "Weather forecast after alarm",
    "routines": "Alarm routines",
    "routine_weather": "routine weather briefing",
    "routine_calendar": "routine calendar briefing",
    "routine_tasks": "routine task briefing",
    "routine_joke": "routine joke",
    "routine_commute": "routine commute briefing",
    "routine_news": "routine news playback",
    "routine_add_action": "routine custom action",
    "routine_save": "routine save",
}


async def handle_alarm_feature_interest(request: Request) -> JSONResponse:
    """Record an explicit tap on a coming-soon Alarm settings row."""
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    try:
        body = await request.json()
    except Exception:
        body = {}
    feature = str(body.get("feature", "") if isinstance(body, dict) else "").strip()
    label = _ALARM_INTEREST_LABELS.get(feature)
    if label is None:
        return JSONResponse({"error": "Unknown alarm feature."}, status_code=400)

    captured = await capture_feedback(
        user_id,
        FeedbackReport(
            category="feature_request",
            about="reminders",
            summary=f"User tapped the coming-soon {label} control in Alarm settings.",
            severity="low",
        ),
        source="mobile_alarm_settings",
    )
    if not captured:
        return JSONResponse({"error": "Temporarily unavailable"}, status_code=503)
    return JSONResponse({"ok": True})


def _reminders_ref(user_id: str):
    return (
        admin_firestore()
        .collection("users")
        .document(user_id)
        .collection("reminders")
    )


def _local_wall_clock(instant: datetime, timezone_name: str) -> str:
    """Render a UTC instant as naive local wall clock in the reminder's zone.

    Falls back to the UTC wall clock on an unknown zone rather than failing the
    request: the device re-resolves this itself, and a wrong-by-an-offset hint is
    still better than dropping the field and leaving the client nothing to anchor
    a timezone move against.
    """
    try:
        zone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warn("reminders: unknown timezone on reminder", {"timezone": timezone_name})
        return instant.replace(tzinfo=None).isoformat()
    return instant.astimezone(zone).replace(tzinfo=None).isoformat()


def _resolve_snooze_trigger(body: dict[str, Any], *, now: datetime) -> datetime | None:
    """When the snoozed alarm should next ring, or None if that moment has passed.

    The device that is ringing arms its own snooze locally and immediately, it
    does not wait for a network round trip at 3 AM, and it must still work in
    airplane mode. So it tells the server the exact time it already armed
    (`next_trigger_at`) rather than a duration, and the server records that
    instead of recomputing from its own clock. Without this, an ack queued
    offline and flushed hours later would set a snooze counted from the flush.

    `snooze_minutes` remains as the fallback for a caller acking in real time.
    """
    raw = str(body.get("next_trigger_at", "") or "").strip()
    if raw:
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        parsed = parsed.astimezone(UTC)
        # Reject a client-supplied time that is already gone, and cap how far
        # ahead an ack may push an alarm: this field is attacker-controllable by
        # the account owner, and a year-out "snooze" is not a snooze.
        if parsed <= now or parsed > now + timedelta(hours=24):
            return None
        return parsed

    try:
        minutes = int(body.get("snooze_minutes", DEFAULT_SNOOZE_MINUTES))
    except (TypeError, ValueError):
        minutes = DEFAULT_SNOOZE_MINUTES
    return now + timedelta(minutes=max(1, min(MAX_SNOOZE_MINUTES, minutes)))


def _to_sync_row(
    doc_id: str,
    data: dict[str, Any],
    default_tone: str,
    voice_slug: str,
) -> dict[str, Any]:
    message = str(data.get("message", ""))[:200]
    local_time = str(data.get("local_time", ""))
    # One concrete slug, already collapsed from (per-alarm override, user
    # default). The device plays what it is told and owns no precedence rule.
    tone = alarm_tones.resolve_tone(data.get("tone"), default_tone)
    return {
        "reminder_id": doc_id,
        "message": message,
        "trigger_at": str(data.get("trigger_at", "")),
        "local_time": local_time,
        "timezone": str(data.get("timezone", "")),
        "snooze_count": int(data.get("snooze_count", 0) or 0),
        "tone": tone,
        # Only the spoken tone needs a clip, and the tag is what the device
        # caches under. It is stable for (message, resolved voice), so a device
        # that already holds this tag fetches nothing: reconcile runs on every
        # app resume, and without this every resume would bill a fresh render.
        "clip_tag": (
            alarm_voice.clip_tag(message, voice_slug)
            if alarm_tones.speaks_aloud(tone)
            else ""
        ),
    }


async def handle_list_alarms(request: Request) -> JSONResponse:
    """GET /reminders/alarms, the complete set of alarms this device should arm.

    Complete, not incremental: the client replaces its local schedule with this
    answer, so an alarm the user cancelled on another device disappears by being
    absent. An incremental feed would need a cursor and would leave a dropped
    page armed forever.

    Queries on `status` alone and filters the rest in memory. A composite index
    on (status, tier, trigger_at) would be faster in theory, but a user has tens
    of reminders, not thousands, and an endpoint the alarm path depends on must
    not be one index deployment away from returning nothing.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    now = datetime.now(UTC)
    horizon = (now + timedelta(days=SYNC_HORIZON_DAYS)).isoformat()

    # Read once for the whole response rather than per row. Both degrade to a
    # safe value on failure and neither blocks the sync itself.
    default_tone = await alarm_tones.user_default_tone(user_id)
    voice_slug = await alarm_voice.resolved_voice_slug(user_id)

    def _fetch() -> list[dict[str, Any]]:
        from google.cloud.firestore_v1.base_query import FieldFilter

        rows: list[dict[str, Any]] = []
        for doc in (
            _reminders_ref(user_id)
            .where(filter=FieldFilter("status", "==", "pending"))
            .limit(500)
            .stream()
        ):
            data = doc.to_dict() or {}
            if not alarm_sync.is_alarm(data):
                continue
            if str(data.get("trigger_at", "")) > horizon:
                continue
            rows.append(_to_sync_row(doc.id, data, default_tone, voice_slug))
        return rows

    try:
        alarms = await asyncio.to_thread(_fetch)
    except Exception as exc:
        # 503, never an empty 200. An empty list is an instruction to DISARM
        # everything, so returning one on a Firestore blip would cancel every
        # alarm the user has. Zero rows and unhealthy must never look identical.
        logger.error("reminders: alarm sync query failed", {
            "user_id": user_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return JSONResponse({"error": "Temporarily unavailable"}, status_code=503)

    alarms.sort(key=lambda row: row["trigger_at"])
    logger.info("reminders: alarm sync served", {
        "user_id": user_id,
        "count": len(alarms),
    })
    return JSONResponse({
        "alarms": alarms,
        # The device compares this to its own clock. A phone whose clock has
        # drifted will otherwise arm every alarm at a consistently wrong moment
        # and there is no other signal that would reveal it.
        "server_time": now.isoformat(),
    })


async def handle_wake_clip(request: Request, reminder_id: str) -> Response:
    """GET /reminders/{id}/wake-clip, Buddy reading this reminder aloud.

    Fetched at ARM time and cached on the device, never at ring time: when the
    alarm fires there is no Flutter engine and possibly no network. See
    ``services/alarm_voice.py`` for why the device caches on an opaque tag
    rather than deciding for itself when to re-render.

    A 404 here is not a broken alarm. Every caller falls back to letting the bed
    tone loop uninterrupted, which still wakes the user, so this returns rather
    than raises on a missing key, a Cartesia outage, or a timeout.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    reminder_id = (reminder_id or "").strip()
    if not reminder_id:
        return JSONResponse({"error": "Missing reminder id."}, status_code=400)

    snapshot = await asyncio.to_thread(_reminders_ref(user_id).document(reminder_id).get)
    if not snapshot.exists:
        return JSONResponse({"error": "Reminder not found."}, status_code=404)
    data = snapshot.to_dict() or {}

    # This endpoint has a real provider cost. A signed-in user may only render a
    # clip for an alarm whose resolved tone is actually Buddy; arbitrary ordinary
    # reminders and alarms using another tone must not become a TTS oracle.
    tone = alarm_tones.resolve_tone(
        data.get("tone"),
        await alarm_tones.user_default_tone(user_id),
    )
    if not alarm_sync.is_alarm(data) or not alarm_tones.speaks_aloud(tone):
        return JSONResponse({"error": "Wake clip not requested."}, status_code=404)

    message = str(data.get("message", ""))[:200]
    label = alarm_voice.spoken_time_label(str(data.get("local_time", "")))

    from ..services.entitlement import get_user_effective_tier

    def _read_voice_id() -> str:
        doc = admin_firestore().collection("users").document(user_id).get()
        return str(((doc.to_dict() or {}).get("settings") or {}).get("tts_voice_id") or "").strip()

    try:
        voice_id = await asyncio.to_thread(_read_voice_id)
        tier = await get_user_effective_tier(user_id)
    except Exception:
        voice_id, tier = "", "free"

    audio = await alarm_voice.render(message, label, voice_id, tier)
    if not audio:
        return JSONResponse({"error": "Clip unavailable."}, status_code=503)

    logger.info("reminders: wake clip served", {
        "user_id": user_id,
        "reminder_id": reminder_id,
        "bytes": len(audio),
    })
    return Response(
        content=audio,
        media_type="audio/mpeg",
        # The device keys its own cache on the clip tag from the sync row, so
        # nothing in between needs to hold this. It is also one user's own
        # reminder read aloud, which no shared cache should ever keep.
        headers={"Cache-Control": "private, no-store"},
    )


async def handle_acknowledge_alarm(request: Request, reminder_id: str) -> JSONResponse:
    """POST /reminders/{id}/ack, settle a ringing alarm.

    ``dismiss`` ends it, ``im_up`` ends it and is the signal that the wake-up
    worked, ``snooze`` re-arms it a few minutes out.
    """
    user_id = resolve_user_id_from_request(request)
    if not user_id:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)

    reminder_id = (reminder_id or "").strip()
    if not reminder_id:
        return JSONResponse({"error": "Missing reminder id."}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    action = str(body.get("action", "") or "").strip().lower()
    if action not in VALID_ACTIONS:
        return JSONResponse(
            {"error": f"action must be one of: {', '.join(sorted(VALID_ACTIONS))}."},
            status_code=400,
        )

    ref = _reminders_ref(user_id).document(reminder_id)
    snapshot = await asyncio.to_thread(ref.get)
    if not snapshot.exists:
        return JSONResponse({"error": "Reminder not found."}, status_code=404)
    existing = snapshot.to_dict() or {}

    now = datetime.now(UTC)
    now_iso = now.isoformat()
    update: dict[str, Any] = {
        "acked_at": now_iso,
        "ack_action": action,
        "ack_platform": str(request.headers.get("X-Aura-Platform", ""))[:24],
    }
    response: dict[str, Any] = {"ok": True, "action": action}

    if action == ACTION_SNOOZE:
        next_trigger = _resolve_snooze_trigger(body, now=now)
        if next_trigger is None:
            # The device already rang, snoozed, and re-armed locally; this ack is
            # just arriving late (queued while the app was closed, flushed on next
            # open) and the snooze it describes has already come and gone. Writing
            # a fresh future time here would resurrect an alarm the user is done
            # with, so settle the row instead. The local schedule, not this
            # request, is what actually controls whether anything rings.
            logger.info("reminders: stale snooze ack settled as dismissed", {
                "user_id": user_id,
                "reminder_id": reminder_id,
            })
            action = ACTION_DISMISS
            update["ack_action"] = ACTION_DISMISS
            update["status"] = "dismissed"
            update["dismissed_at"] = now_iso
            response["action"] = ACTION_DISMISS
            response["reason"] = "snooze_elapsed"
        else:
            timezone_name = str(existing.get("timezone", "")) or "UTC"

        # DELIBERATELY still "pending", and deliberately NOT a "snoozed" status.
        # fetch_due_reminders() selects on status == "pending" and nothing else,
        # so a snoozed row would drop out of the backstop scan permanently and
        # the 3 AM snooze would quietly be the end of the accountability. The
        # Flutter model does declare a `snoozed` value; it has never had a
        # producer and must not get one here. `snooze_count` carries the
        # distinction for display instead.
            update["status"] = "pending"
            update["trigger_at"] = next_trigger.isoformat()
            update["local_time"] = _local_wall_clock(next_trigger, timezone_name)
            update["snooze_count"] = int(existing.get("snooze_count", 0) or 0) + 1
            # Clear the in-flight marker so a row the scheduler had already
            # claimed is eligible again. Without this a snooze on a
            # backstop-delivered alarm would leave `processing_at` set and read
            # as stuck to the sweeper.
            update["processing_at"] = None
            response["trigger_at"] = update["trigger_at"]
            response["snooze_count"] = update["snooze_count"]
    elif action == ACTION_IM_UP:
        update["status"] = "fired"
        update["fired_at"] = now_iso
    else:
        update["status"] = "dismissed"
        update["dismissed_at"] = now_iso

    try:
        await asyncio.to_thread(ref.update, update)
    except Exception as exc:
        logger.error("reminders: ack write failed", {
            "user_id": user_id,
            "reminder_id": reminder_id,
            "action": action,
            "error": str(exc),
        })
        return JSONResponse({"error": "Temporarily unavailable"}, status_code=503)

    # Reach the user's OTHER devices, which are ringing right now off the same
    # local schedule. Detached: the phone in the user's hand has already stopped,
    # and making it wait on an FCM round trip at 3 AM would be its own bug.
    if action == ACTION_SNOOZE:
        # One message, not stop-then-schedule: a re-arm for an id that is
        # currently ringing means "stop and take this new time", which leaves no
        # window where a device has stopped but not yet rescheduled.
        asyncio.create_task(alarm_sync.push_schedule(user_id, {
            **existing, **update, "id": reminder_id,
        }))
    else:
        asyncio.create_task(alarm_sync.push_stop(user_id, reminder_id, action=action))

    logger.info("reminders: alarm acknowledged", {
        "user_id": user_id,
        "reminder_id": reminder_id,
        "action": action,
        "snooze_count": update.get("snooze_count"),
    })

    from ..services.analytics import posthog_client

    async def _capture_alarm_acked() -> None:
        # Await the capture then drain the SDK queue in the same detached task:
        # Cloud Run can freeze the instance once the response returns, so an
        # unflushed queue silently drops the event (mirrors the scheduler and
        # briefing engine flush-after-capture pattern).
        await posthog_client.capture_event(
            distinct_id=user_id,
            event="alarm_acked",
            properties={"action": action, "snooze_count": update.get("snooze_count", 0)},
        )
        await posthog_client.flush()

    asyncio.create_task(_capture_alarm_acked())
    return JSONResponse(response)
