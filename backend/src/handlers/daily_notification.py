"""
Daily notification handlers — three OIDC-gated internal endpoints.

POST /internal/daily-notify/plan-all
    Called by Cloud Scheduler at 6 AM UTC daily.
    Queries Firestore for users with FCM tokens active in the last 7 days,
    calculates per-user delay so each task fires at 7 AM local time,
    and enqueues one Cloud Task per user.

POST /internal/daily-notify/plan/{uid}
    Called by Cloud Tasks (one per user, at their 7 AM local).
    Runs the full planning pipeline: planner → verifier → schedule.

POST /internal/daily-notify/send
    Called by Cloud Tasks at the scheduled send time for each nudge.
    Sends the notification via FCM and updates the daily_plans document.
"""

from __future__ import annotations

import asyncio
import json
import time as _time
from datetime import datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.notification_service import send_notification
from ..services.daily_notification.orchestrator import run_daily_plan
from ..config.settings import settings


# Fan-out all active users
async def handle_plan_all_users() -> dict[str, Any]:
    """Triggered by Cloud Scheduler at 6 AM UTC. Fans out one task per active user."""
    active_user_ids = await _fetch_active_user_ids()

    if not active_user_ids:
        logger.info("daily_notification: no active users found, nothing to schedule")
        return {"scheduled": 0}

    scheduled = 0
    errors = 0
    for user_id in active_user_ids:
        try:
            user_timezone = await _fetch_user_timezone(user_id)
            delay_seconds = _seconds_until_7am_local(user_timezone)
            await asyncio.to_thread(_enqueue_plan_task, user_id, delay_seconds)
            scheduled += 1
        except Exception as exc:
            errors += 1
            logger.warn("daily_notification: failed to enqueue plan task", {
                "user_id": user_id,
                "error": str(exc),
            })

    logger.info("daily_notification: fan-out complete", {
        "total_users": len(active_user_ids),
        "scheduled": scheduled,
        "errors": errors,
    })
    return {"scheduled": scheduled, "errors": errors, "total_users": len(active_user_ids)}


# Plan for a single user
async def handle_plan_one_user(user_id: str) -> dict[str, Any]:
    """Triggered by Cloud Tasks. Runs the full planning pipeline for one user."""
    logger.info("daily_notification: starting plan for user", {"user_id": user_id})
    await run_daily_plan(user_id)
    return {"status": "ok", "user_id": user_id}


# Send a scheduled notification
async def handle_send_nudge(body: dict[str, Any]) -> dict[str, Any]:
    """Triggered by Cloud Tasks at the nudge's scheduled send time.

    Payload: { user_id, plan_date, nudge_slot: "morning_nudge" | "evening_nudge" }
    """
    user_id: str = body.get("user_id", "")
    plan_date: str = body.get("plan_date", "")
    nudge_slot: str = body.get("nudge_slot", "")

    is_nudge_slot = nudge_slot in ("morning_nudge", "evening_nudge")
    is_meeting_reminder_slot = nudge_slot.startswith("meeting_reminder_")
    if not user_id or not plan_date or (not is_nudge_slot and not is_meeting_reminder_slot):
        logger.warn("daily_notification: send_nudge received invalid payload", {"body": body})
        return {"error": "invalid_payload", "status_code": 400}

    # Load the daily plan document
    plan_doc = await _load_daily_plan(user_id, plan_date)
    if not plan_doc:
        logger.warn("daily_notification: daily_plan not found", {
            "user_id": user_id,
            "plan_date": plan_date,
        })
        return {"error": "plan_not_found", "status_code": 503}

    nudge = plan_doc.get(nudge_slot, {})

    # Idempotency: skip if already sent
    if nudge.get("status") == "sent":
        logger.info("daily_notification: nudge already sent, skipping (idempotent)", {
            "user_id": user_id,
            "nudge_slot": nudge_slot,
        })
        return {"skipped": True, "reason": "already_sent"}

    title: str = nudge.get("title", "")
    notification_body: str = nudge.get("body", "")
    opening_chat_message: str = nudge.get("opening_chat_message", "")
    quick_reply_chips: list = nudge.get("quick_reply_chips", [])

    if not title or not notification_body:
        logger.warn("daily_notification: nudge missing title or body", {
            "user_id": user_id,
            "nudge_slot": nudge_slot,
        })
        return {"error": "missing_content", "status_code": 400}

    logger.info("daily_notification: attempting FCM send", {
        "user_id": user_id,
        "nudge_slot": nudge_slot,
        "plan_date": plan_date,
        "title": title,
    })

    fcm_notification_type = "meeting_reminder" if is_meeting_reminder_slot else "daily_nudge"

    # Send via FCM
    result = await send_notification(
        user_id,
        title=title,
        body=notification_body,
        data={
            "notification_type": fcm_notification_type,
            "plan_date": plan_date,
            "nudge_slot": nudge_slot,
            "initial_message": opening_chat_message,
            "quick_reply_chips": json.dumps(quick_reply_chips),
        },
        notification_type=fcm_notification_type,
        priority="high",
        collapse_key=f"{fcm_notification_type}_{nudge_slot}",
    )

    if result.tokens_targeted == 0:
        logger.warn("daily_notification: no FCM tokens found, notification not delivered", {
            "user_id": user_id,
            "nudge_slot": nudge_slot,
            "plan_date": plan_date,
        })
        return {"status": "no_devices", "tokens_targeted": 0, "success_count": 0}

    if not result.delivered:
        logger.error("daily_notification: FCM delivery failed, all tokens rejected", {
            "user_id": user_id,
            "nudge_slot": nudge_slot,
            "plan_date": plan_date,
            "tokens_targeted": result.tokens_targeted,
            "failure_count": result.failure_count,
        })
        return {"error": "fcm_delivery_failed", "status_code": 500}

    sent_at = datetime.now(timezone.utc).isoformat()

    # Update the daily_plan document
    await _update_nudge_status(user_id, plan_date, nudge_slot, "sent", sent_at)

    # Update engagement_guard. Meeting reminders have their own counter so they
    # don't consume the daily nudge quota.
    if is_meeting_reminder_slot:
        await _update_meeting_reminder_engagement_guard(user_id, sent_at)
    else:
        await _update_engagement_guard(user_id, sent_at)

    logger.info("daily_notification: nudge sent", {
        "user_id": user_id,
        "nudge_slot": nudge_slot,
        "plan_date": plan_date,
        "tokens_targeted": result.tokens_targeted,
        "success_count": result.success_count,
    })

    return {
        "status": "sent",
        "tokens_targeted": result.tokens_targeted,
        "success_count": result.success_count,
    }


# Active user discovery
async def _fetch_active_user_ids() -> list[str]:
    """Return user IDs with FCM tokens, skipping users inactive for 7+ days.

    Activity is determined by the most recent query timestamp. Users with no
    queries at all are assumed active (new accounts).
    """
    def _fetch() -> list[str]:
        from google.cloud.firestore_v1.base_query import FieldFilter  # type: ignore

        db = admin_firestore()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()

        # All users with any FCM token, registration date is irrelevant for activity
        token_docs = db.collection_group("fcm_tokens").stream()
        user_ids: set[str] = set()
        for doc in token_docs:
            path_parts = doc.reference.path.split("/")
            if len(path_parts) >= 2:
                user_ids.add(path_parts[1])

        if not user_ids:
            return []

        active: list[str] = []
        for uid in user_ids:
            recent = (
                db.collection("users").document(uid)
                .collection("queries")
                .where(filter=FieldFilter("timestamp", ">=", cutoff))
                .limit(1)
                .stream()
            )
            if any(True for _ in recent):
                active.append(uid)
            else:
                # No queries ever: might be a new account, so include it
                no_queries = not any(
                    True for _ in
                    db.collection("users").document(uid)
                    .collection("queries").limit(1).stream()
                )
                if no_queries:
                    active.append(uid)

        return active

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("daily_notification: failed to fetch active users", {"error": str(exc)})
        return []


async def _fetch_user_timezone(user_id: str) -> str:
    def _fetch() -> str:
        db = admin_firestore()
        doc = db.collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone", "UTC")
        return "UTC"
    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return "UTC"


# Firestore helpers
async def _load_daily_plan(user_id: str, plan_date: str) -> dict | None:
    def _fetch() -> dict | None:
        db = admin_firestore()
        doc = (
            db.collection("users").document(user_id)
            .collection("daily_plans").document(plan_date)
            .get()
        )
        return doc.to_dict() if doc.exists else None
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("daily_notification: failed to load daily_plan", {"error": str(exc)})
        return None


async def _update_nudge_status(
    user_id: str,
    plan_date: str,
    nudge_slot: str,
    status: str,
    sent_at: str,
) -> None:
    def _update() -> None:
        admin_firestore().collection("users").document(user_id)\
            .collection("daily_plans").document(plan_date)\
            .update({
                f"{nudge_slot}.status": status,
                f"{nudge_slot}.sent_at": sent_at,
            })
    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("daily_notification: failed to update nudge status", {"error": str(exc)})


async def _update_engagement_guard(user_id: str, last_engaged_at: str) -> None:
    """Increment proactive_notifications_sent_today and set last_engaged_at."""
    def _update() -> None:
        from google.cloud import firestore as fs  # type: ignore
        db = admin_firestore()
        guard_ref = (
            db.collection("users").document(user_id)
            .collection("engagement_guard").document("state")
        )
        today = datetime.now(timezone.utc).date().isoformat()

        @fs.transactional
        def _txn(transaction: fs.Transaction) -> None:
            snap = guard_ref.get(transaction=transaction)
            guard = snap.to_dict() or {} if snap.exists else {}
            current_date = guard.get("guard_date")
            current_count = guard.get("proactive_notifications_sent_today", 0) if current_date == today else 0
            transaction.set(guard_ref, {
                "last_engaged_at": last_engaged_at,
                "guard_date": today,
                "proactive_notifications_sent_today": current_count + 1,
            }, merge=True)

        _txn(db.transaction())

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("daily_notification: failed to update engagement_guard", {"error": str(exc)})


async def _update_meeting_reminder_engagement_guard(user_id: str, last_engaged_at: str) -> None:
    """Increment meeting_reminders_sent_today in engagement_guard (separate from nudge quota)."""
    def _update() -> None:
        from google.cloud import firestore as fs  # type: ignore
        db = admin_firestore()
        guard_ref = (
            db.collection("users").document(user_id)
            .collection("engagement_guard").document("state")
        )
        today = datetime.now(timezone.utc).date().isoformat()

        @fs.transactional
        def _txn(transaction: fs.Transaction) -> None:
            snap = guard_ref.get(transaction=transaction)
            guard = snap.to_dict() or {} if snap.exists else {}
            current_date = guard.get("guard_date")
            current_count = guard.get("meeting_reminders_sent_today", 0) if current_date == today else 0
            transaction.set(guard_ref, {
                "last_engaged_at": last_engaged_at,
                "guard_date": today,
                "meeting_reminders_sent_today": current_count + 1,
            }, merge=True)

        _txn(db.transaction())

    try:
        await asyncio.to_thread(_update)
    except Exception as exc:
        logger.warn("daily_notification: failed to update meeting reminder engagement_guard", {"error": str(exc)})


# Cloud Task enqueuing 
def _enqueue_plan_task(user_id: str, delay_seconds: int) -> None:
    """Enqueue a Cloud Task to call POST /internal/daily-notify/plan/{uid}."""
    from google.cloud import tasks_v2  # type: ignore
    from google.protobuf import timestamp_pb2  # type: ignore

    client = tasks_v2.CloudTasksClient()
    queue_path = client.queue_path(
        settings.CLOUD_TASKS_PROJECT,
        settings.CLOUD_TASKS_LOCATION,
        settings.CLOUD_TASKS_QUEUE,
    )

    task: dict[str, Any] = {
        "http_request": {
            "http_method": tasks_v2.HttpMethod.POST,
            "url": f"{settings.BACKEND_INTERNAL_URL}/internal/daily-notify/plan/{user_id}",
            "headers": {"Content-Type": "application/json"},
            "body": b"{}",
            "oidc_token": {
                "service_account_email": settings.SCHEDULER_SA_EMAIL,
                "audience": settings.BACKEND_INTERNAL_URL,
            },
        }
    }

    if delay_seconds > 0:
        eta = timestamp_pb2.Timestamp()
        eta.FromSeconds(int(_time.time()) + delay_seconds)
        task["schedule_time"] = eta

    client.create_task(parent=queue_path, task=task)


# Timezone helpers
def _seconds_until_7am_local(user_timezone: str) -> int:
    """Return the number of seconds from now until 7 AM in the user's timezone.
    If 7 AM has already passed today, returns seconds until 7 AM tomorrow.
    """
    try:
        tz = ZoneInfo(user_timezone)
    except (ZoneInfoNotFoundError, Exception):
        tz = ZoneInfo("UTC")

    now_local = datetime.now(tz)
    target = now_local.replace(hour=7, minute=0, second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)

    delay = int((target - now_local).total_seconds())
    # Cloud Tasks minimum delay is 0, so capping at 24 hours as a safety guard
    return max(0, min(delay, 86400))
