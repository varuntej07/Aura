"""
POST /scheduler/tick finds due reminders and sends FCM push notifications.
Called by a cron job (Cloud Scheduler) every minute
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from ..lib.logger import logger
from ..services.notification_rewriter import rewrite_reminder_notification
from ..services.notification_service import send_notification
from ..services.tool_executor import fetch_due_reminders, claim_reminder_for_processing, mark_reminder_fired


def _json(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(payload),
    }


async def handle_scheduler_tick(event: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run one scheduler tick.

    All Firestore / Firebase Admin SDK calls are synchronous (blocking I/O).
    They are dispatched to a thread-pool via `asyncio.to_thread` so they never block the event loop.

    Notifications are sent via the centralized `send_notification` function
    which handles token lookup, FCM multicast, and invalid-token cleanup automatically.
    """
    try:
        from ..services.google_calendar_connector import GoogleCalendarConnector

        renewed_channels, synced_calendars, due = await asyncio.gather(
            asyncio.to_thread(GoogleCalendarConnector.renew_expiring_channels, 10),
            asyncio.to_thread(GoogleCalendarConnector.process_pending_sync_jobs, 20),
            asyncio.to_thread(fetch_due_reminders),
        )

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

        logger.info("Scheduler tick complete", {
            "scanned": len(due),
            "delivered": delivered,
            "calendar_syncs": synced_calendars,
            "renewed_calendar_channels": renewed_channels,
        })
        return _json(200, {
            "scanned": len(due),
            "delivered": delivered,
            "calendar_syncs": synced_calendars,
            "renewed_calendar_channels": renewed_channels,
        })

    except Exception as exc:
        logger.error("Scheduler tick failed", {"error": str(exc)})
        return _json(500, {"error": "Internal server error"})
