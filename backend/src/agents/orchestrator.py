"""
ScheduledAgentOrchestrator — fan-out runner for all domain agents.

Pipeline per invocation:
  1. /internal/agents/tick  → load active user IDs, enqueue one Cloud Task per (agent, user)
  2. /internal/agents/{agentId}/run/{userId}  → run one agent for one user end-to-end:
       a. Load user config + recent feedback from Firestore
       b. Fetch fresh content (HN, arXiv, cricket, job boards, etc.)
       c. Build notification copy via LLM
       d. Send FCM push with agent_id in data payload
       e. Write to agent_nudge_log

All errors are caught and logged — a single failure never blocks other users.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.notification_service import send_notification
from .agent_registry import get_scheduled_agent_registry


# ── Fan-out: schedule one task per (agent, user) ─────────────────────────────

async def orchestrate_all_agents(agent_ids: list[str] | None = None) -> dict[str, Any]:
    """
    Called by POST /internal/agents/tick.
    Loads active users then enqueues a Cloud Task for each (agent, user) pair.
    Returns a summary dict for the response body.
    """
    from ..services.engagement.task_scheduler import get_task_scheduler

    registry = get_scheduled_agent_registry()
    ids_to_run = agent_ids or registry.all_agent_ids
    user_ids = await _load_active_user_ids()

    if not user_ids:
        logger.info("agent_orchestrator: no active users, nothing to schedule")
        return {"agents": ids_to_run, "users": 0, "tasks_enqueued": 0}

    scheduler = get_task_scheduler()
    enqueued = 0

    for agent_id in ids_to_run:
        for user_id in user_ids:
            try:
                await asyncio.to_thread(
                    scheduler.schedule_agent_run,
                    agent_id, user_id,
                )
                enqueued += 1
            except Exception as exc:
                logger.error("agent_orchestrator: failed to enqueue task", {
                    "agent_id": agent_id,
                    "user_id": user_id,
                    "error": str(exc),
                })

    logger.info("agent_orchestrator: tick complete", {
        "agents": ids_to_run,
        "users": len(user_ids),
        "tasks_enqueued": enqueued,
    })
    return {"agents": ids_to_run, "users": len(user_ids), "tasks_enqueued": enqueued}


# ── Per-agent, per-user run ───────────────────────────────────────────────────

async def run_agent_for_user(agent_id: str, user_id: str) -> None:
    """
    Called by POST /internal/agents/{agentId}/run/{userId}.
    Never raises — all errors are logged.
    """
    try:
        await _run(agent_id, user_id)
    except Exception as exc:
        logger.exception("agent_orchestrator: unhandled error in run", {
            "agent_id": agent_id,
            "user_id": user_id,
            "error": str(exc),
        })


async def _run(agent_id: str, user_id: str) -> None:
    registry = get_scheduled_agent_registry()
    agent = registry.get_agent(agent_id)

    # Step 1: Load user config + recent feedback in parallel
    user_config, interaction_history = await asyncio.gather(
        agent.load_user_config(user_id),
        agent.load_interaction_history(user_id, limit=20),
    )

    if not user_config.get("enabled", True):
        logger.info("agent_orchestrator: agent disabled for user", {
            "agent_id": agent_id,
            "user_id": user_id,
        })
        return

    # Step 2: Fetch fresh content
    content = await agent.fetch_data(user_config)

    # Step 3: Judge the content — returns None if nothing is worth notifying
    notification = await agent.build_notification(user_id, content, user_config, interaction_history)

    if notification is None:
        logger.info("agent_orchestrator: judge vetoed — no notification", {
            "agent_id": agent_id,
            "user_id": user_id,
        })
        return

    title = notification.get("title", agent_id)
    body = notification.get("body", "")
    opening_chat_message = notification.get("opening_chat_message", "")

    # Step 4: Send FCM push with agent_id in data payload
    nudge_id = str(uuid.uuid4())
    result = await send_notification(
        user_id,
        title=title,
        body=body,
        data={
            "agent_id": agent_id,
            "nudge_id": nudge_id,
            "opening_chat_message": opening_chat_message,
        },
        notification_type="agent_nudge",
    )

    if not result.delivered:
        logger.info("agent_orchestrator: no devices reached", {
            "agent_id": agent_id,
            "user_id": user_id,
        })
        return

    # Step 5: Write engagement log
    await _write_nudge_log(user_id, agent_id, nudge_id, title, body, opening_chat_message)

    logger.info("agent_orchestrator: nudge sent", {
        "agent_id": agent_id,
        "user_id": user_id,
        "nudge_id": nudge_id,
        "tokens_reached": result.success_count,
    })


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _load_active_user_ids(inactivity_days: int = 7) -> list[str]:
    """Return user IDs that have a registered FCM token seen within inactivity_days."""
    from google.cloud.firestore_v1.base_query import FieldFilter

    cutoff = (datetime.now(timezone.utc) - timedelta(days=inactivity_days)).isoformat()

    def _fetch() -> list[str]:
        db = admin_firestore()
        docs = (
            db.collection_group("fcm_tokens")
            .where(filter=FieldFilter("last_seen", ">=", cutoff))
            .stream()
        )
        user_ids: list[str] = []
        seen: set[str] = set()
        for doc in docs:
            # Path: users/{uid}/fcm_tokens/{token}
            parts = doc.reference.path.split("/")
            if len(parts) >= 2:
                uid = parts[1]
                if uid not in seen:
                    seen.add(uid)
                    user_ids.append(uid)
        return user_ids

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.error("agent_orchestrator: failed to load active users", {"error": str(exc)})
        return []


async def _write_nudge_log(
    user_id: str,
    agent_id: str,
    nudge_id: str,
    title: str,
    body: str,
    opening_chat_message: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    doc = {
        "agent_id": agent_id,
        "nudge_id": nudge_id,
        "title": title,
        "body": body,
        "opening_chat_message": opening_chat_message,
        "status": "sent",
        "created_at": now,
        "sent_at": now,
    }

    def _write() -> None:
        admin_firestore().collection("users").document(user_id)\
            .collection("agent_nudge_log").document(nudge_id).set(doc)

    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.warn("agent_orchestrator: nudge log write failed", {
            "nudge_id": nudge_id,
            "error": str(exc),
        })
