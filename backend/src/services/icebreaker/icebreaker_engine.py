"""The Icebreaker engine — one warm, life-aware opener on ~3 random days a week.

Ridden on the existing per-minute scheduler tick (hourly gate); no new Cloud
Scheduler job. For each active user, on each hourly tick:

  consent gate ─► active-hours gate ─► is today a rolled icebreaker day? ─►
  is this the day's single random target hour? ─► ATOMIC claim (idempotent) ─►
  build free context packet ─► one LLM opener with a reject gate ─►
  unified-budget slot (priority) ─► send ─► record opener + funnel.

Most ticks send nothing — that restraint is the feature. Every step is isolated:
one user's failure is caught and never touches another's, and no step can raise
out of the tick. Gated by ``settings.ICEBREAKER_ENABLED`` (default off).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...config.settings import settings
from ...lib.logger import logger
from ..analytics import posthog_client
from ..analytics.funnel_events import (
    EVENT_ICEBREAKER_SENT,
    NOTIFICATION_ORIGIN_ICEBREAKER,
    PROP_NOTIFICATION_ID,
    PROP_NOTIFICATION_ORIGIN,
)
from ..model_provider import ModelProvider, get_model_provider
from ..notification_budget import try_claim_proactive_slot
from ..notification_service import send_notification
from ..signal_engine.feature_store import list_active_user_ids
from ..signal_engine.scoring import is_within_active_hours
from . import icebreaker_store as store
from .context_bundle import build_context_bundle
from .fields import NOTIFICATION_TYPE_ICEBREAKER
from .icebreaker_framer import generate_opener
from .scheduler_logic import (
    current_week_start_date,
    is_scheduled_today,
    roll_week_dates,
    target_local_hour,
)

# Max users processed simultaneously in one tick (mirrors the scoring loop / reflector).
ICEBREAKER_USER_CONCURRENCY = 10


@dataclass
class IcebreakerTickSummary:
    users_considered: int = 0
    sent: int = 0
    skipped_no_consent: int = 0
    skipped_quiet_hours: int = 0
    skipped_not_scheduled: int = 0
    skipped_not_target_hour: int = 0
    skipped_already_sent: int = 0
    skipped_no_hook: int = 0
    skipped_rejected: int = 0
    skipped_budget: int = 0


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


async def run_icebreaker_tick() -> IcebreakerTickSummary:
    """Public entrypoint, called from the scheduler tick on its hourly gate."""
    summary = IcebreakerTickSummary()
    if not settings.ICEBREAKER_ENABLED:
        return summary

    # Loud guard: the icebreaker shares a daily ceiling with every other proactive
    # decider ONLY when the unified budget is on. Running enabled without it means
    # icebreakers are not spaced against signal-engine / thread / reminder pushes
    # and can stack on the same day — the notification-fatigue path. Fire every
    # tick (mirrors the scoring loop's funnel-blind WARNING) so a misconfigured
    # rollout screams instead of silently spamming users.
    if not settings.UNIFIED_NOTIFICATION_BUDGET_ENABLED:
        logger.warn(
            "icebreaker.engine: ICEBREAKER_ENABLED is on but "
            "UNIFIED_NOTIFICATION_BUDGET_ENABLED is off — icebreakers are NOT "
            "coordinated with other proactive pushes and may stack on the same "
            "day. Enable the unified budget before serving real users.",
            {},
        )

    user_ids = await list_active_user_ids()
    summary.users_considered = len(user_ids)
    if not user_ids:
        return summary

    models = get_model_provider()
    semaphore = asyncio.Semaphore(ICEBREAKER_USER_CONCURRENCY)

    async def _process_with_semaphore(user_id: str) -> None:
        async with semaphore:
            try:
                await _process_one_user(user_id, models, summary)
            except Exception as exc:
                # One user's failure is fully contained — never abort the tick.
                logger.exception("icebreaker.engine: per-user failure", {
                    "user_id": user_id,
                    "error": str(exc),
                })

    await asyncio.gather(*[_process_with_semaphore(uid) for uid in user_ids])

    logger.info("icebreaker.engine: tick complete", {
        "users_considered": summary.users_considered,
        "sent": summary.sent,
        "skipped_no_consent": summary.skipped_no_consent,
        "skipped_quiet_hours": summary.skipped_quiet_hours,
        "skipped_not_scheduled": summary.skipped_not_scheduled,
        "skipped_not_target_hour": summary.skipped_not_target_hour,
        "skipped_already_sent": summary.skipped_already_sent,
        "skipped_no_hook": summary.skipped_no_hook,
        "skipped_rejected": summary.skipped_rejected,
        "skipped_budget": summary.skipped_budget,
    })

    # Drain the funnel queue before Cloud Run freezes the container, or the
    # icebreaker_sent events are silently lost (mirrors the scoring loop / reflector).
    await posthog_client.flush()
    return summary


async def _process_one_user(
    user_id: str,
    models: ModelProvider,
    summary: IcebreakerTickSummary,
) -> None:
    # 1. Consent gate (GDPR). A read failure returns consent=False → fail closed.
    targeting = await store.read_user_targeting(user_id)
    if not targeting.consent_granted:
        summary.skipped_no_consent += 1
        return

    local_now = _local_now(targeting.timezone)

    # 2. Never in the middle of the night, regardless of schedule.
    if not is_within_active_hours(local_now.hour, local_now.minute):
        summary.skipped_quiet_hours += 1
        return

    local_date = local_now.date().isoformat()
    week_start = current_week_start_date(local_now)
    rolled_dates = roll_week_dates(user_id, week_start)

    # 3. Is today one of this week's randomly-chosen icebreaker days?
    if not is_scheduled_today(local_date, rolled_dates):
        summary.skipped_not_scheduled += 1
        return

    # 4. Is this the day's single random target hour? (The engine runs hourly, so
    #    exactly one tick per chosen day passes this gate.)
    if local_now.hour != target_local_hour(user_id, local_date):
        summary.skipped_not_target_hour += 1
        return

    # 5. Atomically claim today's slot. Stamping last_sent_date inside the
    #    transaction is what makes one-per-day idempotent under overlapping ticks.
    claim = await store.plan_and_claim_today(
        user_id,
        local_date=local_date,
        week_start_date=week_start,
        rolled_dates=rolled_dates,
    )
    if not claim.claimed:
        # already_sent_today / not_scheduled_today / error — all just stand down.
        if claim.reason == "already_sent_today":
            summary.skipped_already_sent += 1
        else:
            summary.skipped_not_scheduled += 1
        return

    # 6. Build the free context packet. (Day is now claimed; a later skip burns the
    #    day, which is fine — the cadence is a ceiling, not a quota.)
    context = await build_context_bundle(
        user_id, targeting, local_now, claim.recent_opener_topics
    )
    if not context.has_any_hook():
        summary.skipped_no_hook += 1
        logger.info("icebreaker.engine: nothing to open about today, skipping", {
            "user_id": user_id, "local_date": local_date,
        })
        return

    # 7. One LLM opener with a fail-closed reject gate.
    opener = await generate_opener(models, context)
    if not opener.is_send_worthy:
        summary.skipped_rejected += 1
        logger.info("icebreaker.engine: opener rejected, sending nothing today", {
            "user_id": user_id, "reason": opener.reason,
        })
        return

    # 8. Unified daily budget — priority claim (reserved slot so the content engine
    #    can't starve it). No-op while the budget flag is off.
    budget = await try_claim_proactive_slot(
        user_id, source="icebreaker", user_local_date=local_date, priority=True,
    )
    if not budget.allowed:
        summary.skipped_budget += 1
        logger.info("icebreaker.engine: budget denied the icebreaker slot", {
            "user_id": user_id, "reason": budget.reason,
        })
        return

    # 9. Send.
    notification_id = str(uuid.uuid4())
    sent_at = datetime.now(UTC)
    result = await send_notification(
        user_id,
        title=opener.title,
        body=opener.body,
        data={
            PROP_NOTIFICATION_ID: notification_id,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_ICEBREAKER,
            "opening_chat_message": opener.opening_chat_message,
        },
        notification_type=NOTIFICATION_TYPE_ICEBREAKER,
        collapse_key=f"icebreaker_{local_date}",
    )

    if not result.delivered:
        logger.info("icebreaker.engine: send returned no delivery", {
            "user_id": user_id, "tokens_targeted": result.tokens_targeted,
        })
        return

    # 10. Record the opener (anti-repeat memory) and fire the funnel.
    await store.record_sent_opener(user_id, topic=opener.topic, sent_at=sent_at)
    summary.sent += 1

    await posthog_client.capture_event(
        distinct_id=user_id,
        event=EVENT_ICEBREAKER_SENT,
        properties={
            PROP_NOTIFICATION_ID: notification_id,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_ICEBREAKER,
            "topic": opener.topic,
            "reason": opener.reason,
        },
    )

    logger.info("icebreaker.engine: opener sent", {
        "user_id": user_id,
        "local_date": local_date,
        "topic": opener.topic,
        "total_recent_topics": len(claim.recent_opener_topics) + 1,
    })
