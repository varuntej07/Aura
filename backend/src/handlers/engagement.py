"""
Engagement system HTTP handlers:

POST /internal/engage/orchestrate -> Cloud Task callback (immediate).
POST /internal/engage/notify -> Cloud Task callback (delayed).
POST /internal/engage/responded -> Flutter calls when user taps notification.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from ..lib.logger import logger
from ..services.engagement.agents.re_engagement import ReEngagementAgent
from ..services.engagement.orchestrator import _log_analytics, run_orchestration
from ..services.engagement.task_scheduler import get_task_scheduler
from ..services.firebase import admin_firestore
from ..services.model_provider import get_model_provider
from ..services.notification_service import send_notification


# POST /internal/engage/orchestrate
async def handle_engagement_orchestrate(payload: dict[str, Any]) -> dict[str, Any]:
    user_id: str = payload.get("user_id", "")
    trigger_event: str = payload.get("trigger_event", "")
    trigger_payload: dict = payload.get("trigger_payload") or {}

    if not user_id or not trigger_event:
        return {"error": "user_id and trigger_event are required"}

    await run_orchestration(user_id, trigger_event, trigger_payload)
    return {"ok": True}


# POST /internal/engage/notify
async def handle_engagement_notify(payload: dict[str, Any]) -> dict[str, Any]:
    engagement_id: str = payload.get("engagement_id", "").strip()
    user_id: str = payload.get("user_id", "").strip()
    action: str = payload.get("action", "").strip()

    if not engagement_id or not user_id or not action:
        return {"error": "engagement_id, user_id, and action are required"}

    doc = await _load_engagement_log(user_id, engagement_id)
    if not doc:
        logger.warn("engagement_notify: doc not found", {"engagement_id": engagement_id})
        return {"skipped": True, "reason": "not_found"}

    # Already in a terminal state, nothing to do
    if doc.get("status") in ("responded", "expired"):
        return {"skipped": True, "reason": doc["status"]}

    # Idempotency guard, Cloud Tasks retries must be no-ops
    if action in (doc.get("actions_completed") or []):
        logger.info("engagement_notify: idempotent skip", {
            "engagement_id": engagement_id,
            "action": action,
        })
        return {"skipped": True, "reason": "already_completed"}

    now = datetime.now(UTC)

    if action == "send_first":
        await _handle_send_first(user_id, engagement_id, doc, now)

    elif action == "check_and_reengage":
        await _handle_check_and_reengage(user_id, engagement_id, doc, now)

    elif action == "check_and_expire":
        await _handle_check_and_expire(user_id, engagement_id, doc, now)

    elif action == "expire":
        await _handle_expire(user_id, engagement_id, doc, now)

    else:
        logger.warn("engagement_notify: unknown action", {"action": action})
        return {"error": f"unknown action: {action}"}

    return {"ok": True, "action": action}


# POST /internal/engage/responded
async def handle_engagement_responded(
    user_id: str,
    engagement_id: str,
) -> dict[str, Any]:
    """Mark engagement as responded and cancel any pending re-engagement task.
    user_id is derived from the Firebase Auth token and never trusted from request body.
    """
    if not engagement_id:
        return {"error": "engagement_id is required"}

    doc = await _load_engagement_log(user_id, engagement_id)
    if not doc:
        # The engagement might belong to a different user, returning 404, not 403
        return {"error": "not_found"}

    if doc.get("status") == "responded":
        return {"ok": True, "already": True}

    now = datetime.now(UTC)

    # Cancel pending re-engagement task
    task_name = doc.get("cloud_task_name")
    if task_name:
        scheduler = get_task_scheduler()
        await asyncio.to_thread(scheduler.cancel_task, task_name)

    await _update_engagement_log(user_id, engagement_id, {
        "status": "responded",
        "responded_at": now.isoformat(),
    })

    await _log_analytics(user_id, {
        "event": "tapped",
        "engagement_id": engagement_id,
        "agent_type": doc.get("chosen_agent", ""),
        "tone": doc.get("engagement_context", {}).get("tone", ""),
        "re_engagement_level": doc.get("re_engagement_count", 0),
        "trigger_event": doc.get("trigger_event", ""),
        "suppression_reason": None,
        "timestamp": now.isoformat(),
    })

    logger.info("engagement: responded", {
        "user_id": user_id,
        "engagement_id": engagement_id,
    })
    return {"ok": True}


async def _handle_send_first(
    user_id: str,
    engagement_id: str,
    doc: dict,
    now: datetime,
) -> None:
    """Send the pre-generated notification. Schedule check_and_reengage (+24h)."""
    await send_notification(
        user_id,
        title=doc["notification_title"],
        body=doc["notification_body"],
        data={
            "deep_link": "chat",
            "engagement_id": engagement_id,
            "initial_message": doc["opening_chat_message"],
            "suggested_replies": ",".join(doc.get("suggested_replies") or []),
            "agent_context": doc.get("chosen_agent", ""),
        },
        notification_type="engagement",
        priority="normal",
        collapse_key=f"engagement_{engagement_id}",
    )

    # Schedule check_and_reengage, fires 24h from now
    scheduler = get_task_scheduler()
    next_task = await asyncio.to_thread(
        scheduler.schedule_notification,
        engagement_id, user_id, "check_and_reengage", 24 * 3600,
    )

    await _mark_action_complete(user_id, engagement_id, "send_first", {
        "status": "sent",
        "sent_at": now.isoformat(),
        "cloud_task_name": next_task,
    })

    await _log_analytics(user_id, {
        "event": "sent",
        "engagement_id": engagement_id,
        "agent_type": doc.get("chosen_agent", ""),
        "tone": doc.get("engagement_context", {}).get("tone", ""),
        "re_engagement_level": 0,
        "trigger_event": doc.get("trigger_event", ""),
        "suppression_reason": None,
        "timestamp": now.isoformat(),
    })

    logger.info("engagement: notification sent", {
        "user_id": user_id,
        "engagement_id": engagement_id,
        "title": doc["notification_title"],
    })


async def _handle_check_and_reengage(
    user_id: str,
    engagement_id: str,
    doc: dict,
    now: datetime,
) -> None:
    """Followup #1: if no response in 24h, send gentle re-engagement. (+24h)"""
    if doc.get("status") == "responded":
        return

    re_agent = ReEngagementAgent(get_model_provider())
    copy = await re_agent.generate({
        "escalation_level": 1,
        "original_agent": doc.get("chosen_agent", ""),
        "original_topic": doc.get("engagement_context", {}).get("event_title", "something we talked about"),
        "original_notification_title": doc.get("notification_title", ""),
    })

    await send_notification(
        user_id,
        title=copy.title,
        body=copy.body,
        data={
            "deep_link": "chat",
            "engagement_id": engagement_id,
            "initial_message": copy.opening_chat_message,
            "agent_context": doc.get("chosen_agent", ""),
        },
        notification_type="engagement",
        priority="normal",
        collapse_key=f"engagement_{engagement_id}",
    )

    # Schedule check_and_expire, fires 48h from now
    scheduler = get_task_scheduler()
    next_task = await asyncio.to_thread(
        scheduler.schedule_notification,
        engagement_id, user_id, "check_and_expire", 48 * 3600,
    )

    re_count = doc.get("re_engagement_count", 0) + 1
    await _mark_action_complete(user_id, engagement_id, "check_and_reengage", {
        "re_engagement_count": re_count,
        "last_re_engagement_at": now.isoformat(),
        "cloud_task_name": next_task,
    })

    await _log_analytics(user_id, {
        "event": "re_engaged",
        "engagement_id": engagement_id,
        "agent_type": doc.get("chosen_agent", ""),
        "tone": "check_in",
        "re_engagement_level": 1,
        "trigger_event": doc.get("trigger_event", ""),
        "suppression_reason": None,
        "timestamp": now.isoformat(),
    })


async def _handle_check_and_expire(
    user_id: str,
    engagement_id: str,
    doc: dict,
    now: datetime,
) -> None:
    """Followup #2 (final) — if still no response after 48h more, send general
    check-in then EXPIRE. No further tasks scheduled after this. (Task #5: max 2 followups)
    """
    if doc.get("status") == "responded":
        return

    re_agent = ReEngagementAgent(get_model_provider())
    copy = await re_agent.generate({
        "escalation_level": 2,
        "original_agent": doc.get("chosen_agent", ""),
        "original_topic": doc.get("engagement_context", {}).get("event_title", "recent activity"),
        "original_notification_title": doc.get("notification_title", ""),
    })

    await send_notification(
        user_id,
        title=copy.title,
        body=copy.body,
        data={
            "deep_link": "chat",
            "engagement_id": engagement_id,
            "initial_message": copy.opening_chat_message,
            "agent_context": doc.get("chosen_agent", ""),
        },
        notification_type="engagement",
        priority="normal",
        collapse_key=f"engagement_{engagement_id}",
    )

    # No next task, this is the last touch (followup #2 = chain end)
    re_count = doc.get("re_engagement_count", 0) + 1
    await _mark_action_complete(user_id, engagement_id, "check_and_expire", {
        "re_engagement_count": re_count,
        "last_re_engagement_at": now.isoformat(),
        "status": "expired",          # expire immediately after final followup
        "cloud_task_name": None,      # no more pending tasks
    })

    await _log_analytics(user_id, {
        "event": "re_engaged",
        "engagement_id": engagement_id,
        "agent_type": doc.get("chosen_agent", ""),
        "tone": "check_in",
        "re_engagement_level": 2,
        "trigger_event": doc.get("trigger_event", ""),
        "suppression_reason": None,
        "timestamp": now.isoformat(),
    })


async def _handle_expire(
    user_id: str,
    engagement_id: str,
    doc: dict,
    now: datetime,
) -> None:
    """Safety-valve expire action — marks expired with no notification."""
    await _mark_action_complete(user_id, engagement_id, "expire", {
        "status": "expired",
        "cloud_task_name": None,
    })
    await _log_analytics(user_id, {
        "event": "expired",
        "engagement_id": engagement_id,
        "agent_type": doc.get("chosen_agent", ""),
        "tone": "",
        "re_engagement_level": doc.get("re_engagement_count", 0),
        "trigger_event": doc.get("trigger_event", ""),
        "suppression_reason": None,
        "timestamp": now.isoformat(),
    })


# Firestore helpers
async def _load_engagement_log(user_id: str, engagement_id: str) -> dict | None:
    def _fetch() -> dict | None:
        doc = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("engagement_log").document(engagement_id)
            .get()
        )
        return doc.to_dict() if doc.exists else None
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.error("engagement: load_engagement_log failed", {"error": str(exc)})
        return None


async def _update_engagement_log(user_id: str, engagement_id: str, update: dict) -> None:
    def _write() -> None:
        admin_firestore().collection("users").document(user_id)\
            .collection("engagement_log").document(engagement_id).update(update)
    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.error("engagement: update_engagement_log failed", {"error": str(exc)})


async def _mark_action_complete(
    user_id: str,
    engagement_id: str,
    action: str,
    extra_fields: dict,
) -> None:
    """Atomically append action to actions_completed + merge extra fields."""
    from google.cloud.firestore_v1 import ArrayUnion  # type: ignore
    update = {
        "actions_completed": ArrayUnion([action]),
        **extra_fields,
    }
    await _update_engagement_log(user_id, engagement_id, update)
