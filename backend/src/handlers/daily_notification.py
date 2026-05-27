"""
Daily notification handler — OIDC-gated internal endpoint for meeting reminder delivery.

POST /internal/daily-notify/send
    Called by Cloud Tasks at the scheduled send time for each meeting reminder.
    Sends the notification via FCM and updates the daily_plans document.

Note: plan-all and plan-{uid} endpoints were removed with the LLM-planner. The signal
engine (POST /internal/signal-engine/tick) handles discovery notifications. Calendar
reminders are still scheduled by services/daily_notification/orchestrator.py.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.notification_service import send_notification


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

    sent_at = datetime.now(UTC).isoformat()

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
        today = datetime.now(UTC).date().isoformat()

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
        today = datetime.now(UTC).date().isoformat()

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


