"""The Daily Briefing engine — one synthesized morning digest per user.

Rides the existing per-minute scheduler tick on a 15-minute gate; no new Cloud
Scheduler job. For each active user, on the gated tick:

  consent gate ─> is it the user's local BRIEFING_LOCAL_HOUR? ─> ATOMIC claim
  (idempotent per local date) ─> BriefingAgent.generate (rank + judge + synthesize)
  ─> write the briefing (viewable in-app) ─> unified-budget slot ─> one push ─> funnel.

The claim makes "one briefing per user per local date" hold under overlapping
ticks. Every step is isolated: one user's failure is caught and never touches
another's, and no step can raise out of the tick. The briefing document is written
BEFORE the push attempt, so a budget-denied or token-less user can still open today's
briefing in-app. Gated by ``settings.DAILY_BRIEFING_ENABLED`` (default off).
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
    EVENT_BRIEFING_SENT,
    NOTIFICATION_ORIGIN_BRIEFING,
    PROP_NOTIFICATION_ID,
    PROP_NOTIFICATION_ORIGIN,
)
from ..model_provider import ModelProvider, get_model_provider
from ..notification_budget import try_claim_proactive_slot
from ..notification_service import send_notification
from ..signal_engine.feature_store import list_active_user_ids
from . import briefing_agent
from . import briefing_store as store
from .fields import (
    NOTIFICATION_TYPE_BRIEFING,
    STATUS_FAILED,
    STATUS_SKIPPED,
)

# Max users processed simultaneously in one tick (mirrors the scoring loop / icebreaker).
BRIEFING_USER_CONCURRENCY = 5


@dataclass
class BriefingTickSummary:
    users_considered: int = 0
    sent: int = 0
    skipped_no_consent: int = 0
    skipped_not_target_hour: int = 0
    skipped_already_claimed: int = 0
    skipped_nothing_relevant: int = 0
    skipped_budget: int = 0
    skipped_no_tokens: int = 0


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


async def run_briefing_tick(
    *,
    target_user_id: str | None = None,
    force: bool = False,
) -> BriefingTickSummary:
    """Public entrypoint, called from the scheduler tick on its 15-minute gate.

    Manual/dark-test parameters (never used by the scheduled call):
      target_user_id — process ONLY this uid instead of the active-user fan-out, so
        a test can target one phone with zero chance of touching another user.
      force — bypass the local-hour gate and the once-per-day claim, so a briefing
        can be generated + sent on demand (and re-generated) regardless of the time.
    A targeted run also bypasses the DAILY_BRIEFING_ENABLED flag so it can be
    triggered while the feature is still dark for everyone else.
    """
    summary = BriefingTickSummary()
    if target_user_id is None and not settings.DAILY_BRIEFING_ENABLED:
        return summary

    if target_user_id is not None:
        user_ids = [target_user_id]
    else:
        user_ids = await list_active_user_ids()
    summary.users_considered = len(user_ids)
    if not user_ids:
        return summary

    models = get_model_provider()
    semaphore = asyncio.Semaphore(BRIEFING_USER_CONCURRENCY)

    async def _process_with_semaphore(user_id: str) -> None:
        async with semaphore:
            try:
                await _process_one_user(user_id, models, summary, force=force)
            except Exception as exc:
                # One user's failure is fully contained — never abort the tick.
                logger.exception("briefing.engine: per-user failure", {
                    "user_id": user_id,
                    "error": str(exc),
                })

    await asyncio.gather(*[_process_with_semaphore(uid) for uid in user_ids])

    logger.info("briefing.engine: tick complete", {
        "users_considered": summary.users_considered,
        "sent": summary.sent,
        "skipped_no_consent": summary.skipped_no_consent,
        "skipped_not_target_hour": summary.skipped_not_target_hour,
        "skipped_already_claimed": summary.skipped_already_claimed,
        "skipped_nothing_relevant": summary.skipped_nothing_relevant,
        "skipped_budget": summary.skipped_budget,
        "skipped_no_tokens": summary.skipped_no_tokens,
    })

    # Drain the funnel queue before Cloud Run freezes the container, or the
    # briefing_sent events are silently lost (mirrors the scoring loop / icebreaker).
    await posthog_client.flush()
    return summary


async def _process_one_user(
    user_id: str,
    models: ModelProvider,
    summary: BriefingTickSummary,
    *,
    force: bool = False,
) -> None:
    # 1. Consent gate (GDPR). A read failure returns consent=False → fail closed.
    #    Enforced even on a forced test run — the briefing is behavioural content.
    targeting = await store.read_user_targeting(user_id)
    if not targeting.consent_granted:
        summary.skipped_no_consent += 1
        return

    local_now = _local_now(targeting.timezone)

    # 2. Only generate during the user's local morning hour. The 15-min fan-out runs
    #    ~4 times in that hour; the claim ensures exactly one of them generates.
    #    A forced manual run bypasses this so it works at any time of day.
    if not force and local_now.hour != settings.BRIEFING_LOCAL_HOUR:
        summary.skipped_not_target_hour += 1
        return

    local_date = local_now.date().isoformat()

    # 3. Atomically claim today's slot BEFORE the LLM call so a concurrent tick that
    #    finds the doc already present stands down (no double-generate, no double-push).
    #    A forced manual run skips the claim so it can re-generate today's briefing.
    if not force:
        claim = await store.claim_today(user_id, local_date=local_date)
        if not claim.claimed:
            summary.skipped_already_claimed += 1
            return

    # 4. The middle man: rank the pool, judge relevance, synthesize the narrative.
    try:
        result = await briefing_agent.generate(models, user_id, targeting, local_now)
    except Exception as exc:
        await store.mark_terminal(user_id, local_date=local_date, status=STATUS_FAILED)
        logger.warn("briefing.engine: generation raised, marked failed", {
            "user_id": user_id, "error": str(exc),
        })
        return

    if result is None:
        # Nothing worth sending today (no ranked items, or model judged none relevant).
        await store.mark_terminal(user_id, local_date=local_date, status=STATUS_SKIPPED)
        summary.skipped_nothing_relevant += 1
        return

    # 5. Persist the briefing FIRST so it is viewable in-app even if the push is
    #    later budget-denied or the user has no FCM token.
    await store.write_briefing(
        user_id,
        local_date=local_date,
        narrative=result.narrative,
        chat_seed_message=result.chat_seed_message,
        sources=result.sources,
    )

    # 6. Unified daily budget — priority claim so a morning briefing the user expects
    #    is not starved by the content engine. No-op while the budget flag is off.
    budget = await try_claim_proactive_slot(
        user_id, source="daily_briefing", user_local_date=local_date, priority=True,
    )
    if not budget.allowed:
        summary.skipped_budget += 1
        logger.info("briefing.engine: budget denied the briefing push (briefing still viewable)", {
            "user_id": user_id, "reason": budget.reason,
        })
        return

    # 7. Send the single morning push. Tapping it deep-links to the briefing screen.
    notification_id = str(uuid.uuid4())
    result_send = await send_notification(
        user_id,
        title=result.push_title,
        body=result.push_body,
        data={
            PROP_NOTIFICATION_ID: notification_id,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_BRIEFING,
            "deep_link": "briefing",
            "briefing_date": local_date,
        },
        notification_type=NOTIFICATION_TYPE_BRIEFING,
        collapse_key=f"briefing_{local_date}",
    )

    if not result_send.delivered:
        summary.skipped_no_tokens += 1
        logger.info("briefing.engine: send returned no delivery (briefing still viewable)", {
            "user_id": user_id, "tokens_targeted": result_send.tokens_targeted,
        })
        return

    summary.sent += 1

    await posthog_client.capture_event(
        distinct_id=user_id,
        event=EVENT_BRIEFING_SENT,
        properties={
            PROP_NOTIFICATION_ID: notification_id,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_BRIEFING,
            "local_date": local_date,
            "sources_used": len(result.sources),
        },
    )

    logger.info("briefing.engine: briefing sent", {
        "user_id": user_id,
        "local_date": local_date,
        "sources_used": len(result.sources),
    })
