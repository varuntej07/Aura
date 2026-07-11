"""Icebreaker post-send bookkeeping.

P2 cut-over: the per-user opener pipeline (consent + cadence gating, the atomic
per-day claim, the free-context opener) moved into
``reactive.agents.icebreaker.IcebreakerOpenerAgent`` and runs inside the Self-Heal
Envelope, dispatched by the orchestrator off a ``tick`` event. The old
``run_icebreaker_tick`` fan-out is gone. What remains here is the one piece that is
NOT dispatch: ``on_icebreaker_delivered``, the bookkeeping the funnel drain runs on
a real send (anti-repeat memory + the funnel event).
"""

from __future__ import annotations

from datetime import UTC, datetime

from ...lib.logger import logger
from ..analytics import posthog_client
from ..analytics.funnel_events import (
    EVENT_ICEBREAKER_SENT,
    NOTIFICATION_ORIGIN_ICEBREAKER,
    PROP_NOTIFICATION_ID,
    PROP_NOTIFICATION_ORIGIN,
)
from ..notification_service import NotificationResult
from ..notifications.proposal import NotificationProposal
from . import icebreaker_store as store


async def on_icebreaker_delivered(
    proposal: NotificationProposal, result: NotificationResult
) -> None:
    """Post-send bookkeeping for an icebreaker the drain actually DELIVERED: record the
    opener topic (anti-repeat memory) and fire the funnel event. Runs in the drain via
    post_send.dispatch_post_send, so both key off a real send. Never raises."""
    if not result.delivered:
        return
    user_id = proposal.user_id
    data = proposal.data or {}
    topic = data.get("topic", "")

    await store.record_sent_opener(user_id, topic=topic, sent_at=datetime.now(UTC))

    await posthog_client.capture_event(
        distinct_id=user_id,
        event=EVENT_ICEBREAKER_SENT,
        properties={
            PROP_NOTIFICATION_ID: data.get(PROP_NOTIFICATION_ID, ""),
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_ICEBREAKER,
            "topic": topic,
            "reason": data.get("reason", ""),
        },
    )

    logger.info("icebreaker.engine: opener delivered", {
        "user_id": user_id, "topic": topic,
    })
