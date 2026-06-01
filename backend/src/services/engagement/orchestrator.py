"""
EngagementOrchestrator — runs inside a Cloud Task invocation.

Pipeline per trigger event:
  1. Load user context from Firestore in parallel
  2. DecisionEngine.decide() — zero LLM, pure logic
  3. Atomically claim engagement slot (Firestore transaction) — TOCTOU fix
  4. AgentRegistry.get_specialist(agent).generate(context) — one Gemini Flash call
  5. Write engagement_log to Firestore
  6. Schedule delayed notification task (Cloud Tasks)
  7. Log analytics event

This runs in ~300ms (parallel Firestore reads + ~200ms Gemini call).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from google.cloud import firestore as fs  # type: ignore
from langfuse import observe

from ...lib.logger import logger
from ...services.firebase import admin_firestore
from .agent_registry import get_agent_registry
from .decision_engine import (
    MAX_DAILY_PROACTIVE_NOTIFICATIONS,
    MIN_HOURS_BETWEEN_REACTIVE_NOTIFICATIONS,
    decide,
)
from .task_scheduler import get_task_scheduler

# ── Public entry point ────────────────────────────────────────────────────────

async def run_orchestration(
    user_id: str,
    trigger_event: str,
    trigger_payload: dict[str, Any],
) -> None:
    """Called by handle_engagement_orchestrate(). Never raises — all errors are logged."""
    try:
        await _orchestrate(user_id, trigger_event, trigger_payload)
    except Exception as exc:
        logger.exception("orchestrator: unhandled error", {
            "user_id": user_id,
            "trigger_event": trigger_event,
            "error": str(exc),
        })


# ── Core pipeline ─────────────────────────────────────────────────────────────

@observe(name="engagement_orchestration")
async def _orchestrate(
    user_id: str,
    trigger_event: str,
    trigger_payload: dict[str, Any],
) -> None:
    now = datetime.now(UTC)

    # Step 1: Load context in parallel
    engagement_guard, memories, user_timezone = await asyncio.gather(
        _load_engagement_guard(user_id),
        _load_memories(user_id),
        _load_user_timezone(user_id),
    )

    context: dict[str, Any] = {
        "engagement_guard": engagement_guard,
        "memories": memories,
        "user_timezone": user_timezone,
    }

    # Step 2: Deterministic decision — zero LLM calls
    decision = decide(trigger_event, trigger_payload, context)

    if not decision.should_engage:
        logger.info("orchestrator: suppressed", {
            "user_id": user_id,
            "trigger_event": trigger_event,
            "reason": decision.suppression_reason,
        })
        await _log_analytics(user_id, {
            "event": "suppressed",
            "engagement_id": "",
            "agent_type": decision.chosen_agent,
            "tone": decision.tone,
            "trigger_event": trigger_event,
            "suppression_reason": decision.suppression_reason,
            "re_engagement_level": 0,
            "timestamp": now.isoformat(),
        })
        return

    # Step 3: Atomically claim engagement slot — prevents TOCTOU race
    is_interaction_triggered = trigger_event == "calendar_event"
    claimed, suppression_reason = await asyncio.to_thread(
        _claim_engagement_slot_sync, user_id, now, is_interaction_triggered
    )
    if not claimed:
        logger.info("orchestrator: slot not claimed", {
            "user_id": user_id,
            "reason": suppression_reason,
        })
        await _log_analytics(user_id, {
            "event": "suppressed",
            "engagement_id": "",
            "agent_type": decision.chosen_agent,
            "tone": decision.tone,
            "trigger_event": trigger_event,
            "suppression_reason": suppression_reason,
            "re_engagement_level": 0,
            "timestamp": now.isoformat(),
        })
        return

    # Step 4: Generate notification copy — one Gemini Flash call
    registry = get_agent_registry()
    agent = registry.get_specialist(decision.chosen_agent)
    agent_context = {**decision.engagement_context, "tone": decision.tone}
    notification = await agent.generate(agent_context)

    # Step 5: Write engagement_log
    engagement_id = str(uuid.uuid4())
    log_doc: dict[str, Any] = {
        "trigger_event": trigger_event,
        "trigger_payload": trigger_payload,
        "chosen_agent": decision.chosen_agent,
        "notification_title": notification.title,        # type: ignore[union-attr]
        "notification_body": notification.body,          # type: ignore[union-attr]
        "opening_chat_message": notification.opening_chat_message,  # type: ignore[union-attr]
        "suggested_replies": notification.suggested_replies,        # type: ignore[union-attr]
        "engagement_context": decision.engagement_context,
        "cloud_task_name": None,
        "actions_completed": [],
        "status": "scheduled",
        "created_at": now.isoformat(),
        "sent_at": None,
        "responded_at": None,
        "re_engagement_count": 0,
        "last_re_engagement_at": None,
    }
    await asyncio.to_thread(_write_engagement_log_sync, user_id, engagement_id, log_doc)

    # Step 6: Schedule delayed notification task
    delay_seconds = decision.delay_minutes * 60
    scheduler = get_task_scheduler()
    task_name = await asyncio.to_thread(
        scheduler.schedule_notification,
        engagement_id, user_id, "send_first", delay_seconds,
    )
    await asyncio.to_thread(
        _update_engagement_log_sync,
        user_id, engagement_id, {"cloud_task_name": task_name},
    )

    # Step 7: Analytics
    await _log_analytics(user_id, {
        "event": "scheduled",
        "engagement_id": engagement_id,
        "agent_type": decision.chosen_agent,
        "tone": decision.tone,
        "trigger_event": trigger_event,
        "suppression_reason": None,
        "re_engagement_level": 0,
        "timestamp": now.isoformat(),
    })

    logger.info("orchestrator: engagement scheduled", {
        "user_id": user_id,
        "engagement_id": engagement_id,
        "agent": decision.chosen_agent,
        "tone": decision.tone,
        "delay_minutes": decision.delay_minutes,
    })


# ── Firestore transaction — atomic slot claim (Task #4) ───────────────────────

def _claim_engagement_slot_sync(
    user_id: str,
    now: datetime,
    is_interaction_triggered: bool,
) -> tuple[bool, str | None]:
    """Atomic read-check-increment on the engagement_guard document.

    Returns (claimed, suppression_reason).
    Runs in a thread (blocking Firestore SDK).
    """
    db = admin_firestore()
    guard_ref = (
        db.collection("users")
        .document(user_id)
        .collection("engagement_guard")
        .document("state")
    )

    @fs.transactional
    def _txn(transaction: fs.Transaction) -> tuple[bool, str | None]:
        snap = guard_ref.get(transaction=transaction)
        guard = snap.to_dict() or {} if snap.exists else {}
        today = now.date().isoformat()

        # Re-check time gap inside transaction (authoritative check)
        last_engaged = guard.get("last_engaged_at")
        if last_engaged:
            try:
                hours_ago = (now - datetime.fromisoformat(last_engaged)).total_seconds() / 3600
                if hours_ago < MIN_HOURS_BETWEEN_REACTIVE_NOTIFICATIONS:
                    return False, "too_recent"
            except ValueError:
                pass

        # Daily cap — only proactive engagements count toward the cap;
        # interaction-triggered (calendar) are exempt
        if not is_interaction_triggered:
            sent_today = guard.get("proactive_notifications_sent_today", 0) if guard.get("guard_date") == today else 0
            if sent_today >= MAX_DAILY_PROACTIVE_NOTIFICATIONS:
                return False, "daily_cap"

        # Claim the slot atomically
        update: dict[str, Any] = {
            "last_engaged_at": now.isoformat(),
            "guard_date": today,
        }
        if is_interaction_triggered:
            current = guard.get("user_action_notifications_sent_today", 0) if guard.get("guard_date") == today else 0
            update["user_action_notifications_sent_today"] = current + 1
        else:
            current = guard.get("proactive_notifications_sent_today", 0) if guard.get("guard_date") == today else 0
            update["proactive_notifications_sent_today"] = current + 1

        transaction.set(guard_ref, update, merge=True)
        return True, None

    return _txn(db.transaction())


# ── Context loaders ───────────────────────────────────────────────────────────

async def _load_engagement_guard(user_id: str) -> dict:
    def _fetch() -> dict:
        db = admin_firestore()
        doc = (
            db.collection("users").document(user_id)
            .collection("engagement_guard").document("state")
            .get()
        )
        return doc.to_dict() if doc.exists else {}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("orchestrator: engagement_guard load failed", {"error": str(exc)})
        return {}


async def _load_user_timezone(user_id: str) -> str:
    """Returns the user's IANA timezone string, defaulting to 'UTC' if not set."""
    def _fetch() -> str:
        db = admin_firestore()
        doc = db.collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone", "UTC")
        return "UTC"
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("orchestrator: user_timezone load failed", {"error": str(exc)})
        return "UTC"


async def _load_memories(user_id: str, limit: int = 10) -> list[dict]:
    def _fetch() -> list[dict]:
        db = admin_firestore()
        docs = (
            db.collection("users").document(user_id)
            .collection("memories")
            .limit(limit)
            .stream()
        )
        return [{"id": d.id, **d.to_dict()} for d in docs]
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("orchestrator: memories load failed", {"error": str(exc)})
        return []


# ── Firestore write helpers ───────────────────────────────────────────────────

def _write_engagement_log_sync(user_id: str, engagement_id: str, doc: dict) -> None:
    admin_firestore().collection("users").document(user_id)\
        .collection("engagement_log").document(engagement_id).set(doc)


def _update_engagement_log_sync(user_id: str, engagement_id: str, update: dict) -> None:
    admin_firestore().collection("users").document(user_id)\
        .collection("engagement_log").document(engagement_id).update(update)


# ── Analytics ─────────────────────────────────────────────────────────────────

async def _log_analytics(user_id: str, event: dict[str, Any]) -> None:
    """Append-only analytics event. Never raises."""
    def _write() -> None:
        event_id = str(uuid.uuid4())
        admin_firestore().collection("users").document(user_id)\
            .collection("engagement_analytics").document(event_id).set(event)
    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("orchestrator: analytics write failed", {"error": str(exc)})
