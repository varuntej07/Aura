"""
POST /scheduler/tick finds due reminders and sends FCM push notifications.
Called by a cron job (Cloud Scheduler) every minute.

Periodic work piggybacked here (avoids creating extra Cloud Scheduler jobs):
  minute % 30 == 0  — calendar fallback sync for all users
  hour == 1, minute == 30  — daily plan fan-out (= 07:00 IST)
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ..lib.logger import logger
from ..services.notification_budget import record_committed_send
from ..services.notification_rewriter import rewrite_reminder_notification
from ..services.notification_service import send_notification
from ..services.tool_executor import (
    claim_reminder_for_processing,
    fetch_due_reminders,
    mark_reminder_fired,
)


def _json(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


async def _fan_out_daily_plans() -> None:
    """Run the calendar reminder pipeline for every active user.

    Triggered once per day at 01:30 UTC (= 07:00 IST) from handle_scheduler_tick.
    run_daily_plan is idempotent per calendar-date, so a retry on the same day
    produces no duplicate work.

    Runs as fire-and-forget via asyncio.create_task so the scheduler tick
    returns its 200 before the LLM-heavy plan generation completes.
    """
    from ..services.daily_notification.orchestrator import run_daily_plan
    from ..services.signal_engine.feature_store import list_active_user_ids

    try:
        user_ids = await list_active_user_ids()
    except Exception as exc:
        logger.error("scheduler: daily plan fan-out — failed to load active users", {"error": str(exc)})
        return

    if not user_ids:
        logger.info("scheduler: daily plan fan-out — no active users")
        return

    # Cap concurrent LLM calls to avoid Cloud Run OOM during the morning burst.
    semaphore = asyncio.Semaphore(5)

    async def _plan_one(uid: str) -> None:
        async with semaphore:
            await run_daily_plan(uid)

    await asyncio.gather(*[_plan_one(uid) for uid in user_ids])

    logger.info("scheduler: daily plan fan-out complete", {"users": len(user_ids)})


async def _run_thread_reflection() -> None:
    """Hourly curiosity follow-up pass over all active users.

    Fire-and-forget so the scheduler tick returns its 200 before the LLM-bound
    reflection runs. A no-op while THREAD_ENGINE_ENABLED is off, and internally
    isolated per user, so it can never delay or fail the reminder tick.
    """
    from ..services.threads.thread_reflector import run_reflection_tick

    try:
        await run_reflection_tick()
    except Exception as exc:
        logger.error("scheduler: thread reflection tick failed", {"error": str(exc)})


async def _run_icebreaker() -> None:
    """Hourly icebreaker pass over all active users.

    Fire-and-forget so the scheduler tick returns its 200 before the LLM-bound
    opener generation runs. A no-op while ICEBREAKER_ENABLED is off, and
    internally isolated per user, so it can never delay or fail the reminder tick.
    """
    from ..services.icebreaker.icebreaker_engine import run_icebreaker_tick

    try:
        await run_icebreaker_tick()
    except Exception as exc:
        logger.error("scheduler: icebreaker tick failed", {"error": str(exc)})


async def handle_scheduler_tick(event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one scheduler tick.

    All Firestore / Firebase Admin SDK calls are synchronous (blocking I/O).
    They are dispatched to a thread-pool via `asyncio.to_thread` so they never
    block the event loop.

    Notifications are sent via the centralized `send_notification` function
    which handles token lookup, FCM multicast, and invalid-token cleanup automatically.
    """
    try:
        from ..services.google_calendar_connector import GoogleCalendarConnector

        now_utc = datetime.now(UTC)
        now_minute = now_utc.minute

        # Periodic fallback sync every 30 min. Catches events missed when:
        #   - Google push notification was dropped (documented ~small% rate)
        #   - Watch channel expired between 6-hour renewal windows
        #   - A transient scheduler failure delayed channel renewal past expiry
        periodic_sync_coro = (
            asyncio.to_thread(GoogleCalendarConnector.sync_all_connected_users)
            if now_minute % 30 == 0
            else asyncio.sleep(0)
        )

        renewed_channels, synced_calendars, due, periodic_sync_result = await asyncio.gather(
            asyncio.to_thread(GoogleCalendarConnector.renew_expiring_channels, 10),
            asyncio.to_thread(GoogleCalendarConnector.process_pending_sync_jobs, 20),
            asyncio.to_thread(fetch_due_reminders),
            periodic_sync_coro,
        )

        # Daily plan fan-out at 01:30 UTC = 07:00 IST.
        # Fire-and-forget: the tick returns 200 immediately; the LLM plan runs in
        # the background without blocking the Cloud Scheduler timeout window.
        if now_utc.hour == 1 and now_minute == 30:
            asyncio.create_task(_fan_out_daily_plans())

        # Curiosity follow-up reflection, once an hour. Fire-and-forget; gated
        # internally by THREAD_ENGINE_ENABLED so this is a cheap no-op until the
        # full thread path (client pill rendering + reply ingest) ships.
        if now_minute == 0:
            asyncio.create_task(_run_thread_reflection())

        # Icebreaker openers, once an hour at minute 15 (offset from the thread
        # reflector at minute 0 so the two LLM passes never burst together).
        # Fire-and-forget; gated internally by ICEBREAKER_ENABLED so this is a
        # cheap no-op until the full icebreaker path is dogfooded on a dark candidate.
        if now_minute == 15:
            asyncio.create_task(_run_icebreaker())

        delivered = 0

        for item in due:
            user_id: str = item["userId"]
            reminder_id: str = item["reminderId"]
            data: dict[str, Any] = item["data"]

            try:
                # Atomically claim the reminder before any slow work (model call, FCM).
                # If another scheduler tick already claimed it, skip — prevents duplicate fires.
                claimed = await asyncio.to_thread(claim_reminder_for_processing, user_id, reminder_id)
                if not claimed:
                    logger.info("Reminder already claimed by concurrent tick, skipping", {
                        "user_id": user_id,
                        "reminder_id": reminder_id,
                    })
                    continue

                raw_message = str(data.get("message", "Reminder due now"))
                body = await rewrite_reminder_notification(raw_message)

                result = await send_notification(
                    user_id,
                    title="Buddy Reminder",
                    body=body,
                    data={
                        "reminder_id": reminder_id,
                        "created_via": str(data.get("created_via", "voice")),
                    },
                    notification_type="reminder",
                    priority="high",
                    # Collapse prevents duplicate banners if the scheduler fires more than once before the user dismisses.
                    collapse_key=f"reminder_{reminder_id}",
                    apns_category="BUDDY_REMINDER",
                )

                if result.delivered:
                    await asyncio.to_thread(mark_reminder_fired, user_id, reminder_id)
                    # Committed send: never blocked, but recorded so a proactive
                    # push is spaced away from it (no-op while the flag is off).
                    await record_committed_send(user_id, source="reminder")
                    delivered += 1
                    logger.info("Reminder delivered", {
                        "user_id": user_id,
                        "reminder_id": reminder_id,
                        "tokens_targeted": result.tokens_targeted,
                        "success_count": result.success_count,
                    })
                else:
                    logger.warn("Reminder not delivered — no valid tokens", {
                        "user_id": user_id,
                        "reminder_id": reminder_id,
                        "tokens_targeted": result.tokens_targeted,
                    })

            except Exception as exc:
                logger.error("Failed to deliver reminder", {
                    "user_id": user_id,
                    "reminder_id": reminder_id,
                    "error": str(exc),
                })

        periodic_synced = (
            (periodic_sync_result or {}).get("users_synced", 0)
            if isinstance(periodic_sync_result, dict)
            else 0
        )

        logger.info("Scheduler tick complete", {
            "scanned": len(due),
            "delivered": delivered,
            "calendar_syncs": synced_calendars,
            "renewed_calendar_channels": renewed_channels,
            "periodic_sync_users": periodic_synced,
        })
        return _json(200, {
            "scanned": len(due),
            "delivered": delivered,
            "calendar_syncs": synced_calendars,
            "renewed_calendar_channels": renewed_channels,
            "periodic_sync_users": periodic_synced,
        })

    except Exception as exc:
        logger.error("Scheduler tick failed", {"error": str(exc)})
        return _json(500, {"error": "Internal server error"})
