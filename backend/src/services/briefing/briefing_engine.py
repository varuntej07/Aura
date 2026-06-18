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
briefing in-app.
"""

from __future__ import annotations

import asyncio
import time
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
from ..tracking import tracking_store
from . import briefing_agent
from . import briefing_store as store
from .fields import (
    NOTIFICATION_TYPE_BRIEFING,
    STATUS_FAILED,
    STATUS_READY,
    STATUS_SKIPPED,
)

# Per-user in-process debounce for the in-app force-refresh (mirrors world_briefing's
# cooldown). Best-effort at beta scale (one instance); never a correctness gate.
_user_refresh_at: dict[str, float] = {}

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
    skipped_tracker_today: int = 0


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


def _tracker_fired_today(latest_update_at: datetime | None, local_now: datetime) -> bool:
    """True when a tracker already delivered to this user on their local date today."""
    if latest_update_at is None:
        return False
    return latest_update_at.astimezone(local_now.tzinfo).date() == local_now.date()


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
    """
    summary = BriefingTickSummary()
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
        "skipped_tracker_today": summary.skipped_tracker_today,
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
    #    later suppressed, budget-denied, or the user has no FCM token.
    await store.write_briefing(
        user_id,
        local_date=local_date,
        narrative=result.narrative,
        chat_seed_message=result.chat_seed_message,
        sources=result.sources,
        items=result.items,
    )

    # 6. Suppress the PUSH (not the briefing) when a topic tracker already notified this
    #    user today — they're getting proactive news from the tracker, so the briefing
    #    push would stack a second notification on the same day. Still fully viewable.
    latest_tracker = await tracking_store.latest_tracker_update_at(user_id)
    if _tracker_fired_today(latest_tracker, local_now):
        summary.skipped_tracker_today += 1
        logger.info("briefing.engine: tracker fired today, skipping push (briefing viewable)", {
            "user_id": user_id,
        })
        return

    # 7. Unified daily budget — priority claim so a morning briefing the user expects
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

    # 8. Send the single morning push. Tapping it deep-links to the briefing screen.
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


async def generate_on_demand(user_id: str, *, force: bool = False) -> store.StoredBriefing | None:
    """Generate and PERSIST today's briefing for one user, on demand from the screen.

    This is what makes the briefing show news straight away (and survive a reopen) even
    before the morning scheduled tick has run: it writes the same `daily_briefing/{date}`
    doc the tick does, so `GET /briefing/today` reads it back. No push (the user is already
    looking at it).

    force=False returns today's existing ready briefing untouched if present (so opening
    the tab twice doesn't regenerate); otherwise it generates one. force=True is the
    refresh button: it regenerates today's briefing, debounced so rapid taps don't spam
    the LLM. Returns the briefing, or the existing one (or None) when there's nothing new
    to generate, so the caller never loses what was already there.
    """
    targeting = await store.read_user_targeting(user_id)
    if not targeting.consent_granted:
        return None

    local_now = _local_now(targeting.timezone)
    local_date = local_now.date().isoformat()

    existing = await store.get_briefing(user_id, local_date=local_date)
    existing_ready = existing if (existing and existing.status == STATUS_READY) else None

    if not force:
        if existing_ready is not None:
            return existing_ready
    else:
        last = _user_refresh_at.get(user_id)
        if last is not None and (time.monotonic() - last) < settings.BRIEFING_REFRESH_COOLDOWN_SECONDS:
            return existing_ready

    models = get_model_provider()
    try:
        result = await briefing_agent.generate(models, user_id, targeting, local_now)
    except Exception as exc:
        logger.warn("briefing.engine: on-demand generation raised", {
            "user_id": user_id, "error": str(exc),
        })
        return existing_ready

    if result is None:
        # Nothing to assemble right now (empty pool); keep whatever was already there.
        return existing_ready

    await store.write_briefing(
        user_id,
        local_date=local_date,
        narrative=result.narrative,
        chat_seed_message=result.chat_seed_message,
        sources=result.sources,
        items=result.items,
    )
    if force:
        _user_refresh_at[user_id] = time.monotonic()

    logger.info("briefing.engine: on-demand briefing written", {
        "user_id": user_id, "local_date": local_date, "force": force,
        "items": len(result.items),
    })
    return store.StoredBriefing(
        local_date=local_date,
        status=STATUS_READY,
        narrative=result.narrative,
        chat_seed_message=result.chat_seed_message,
        sources=result.sources,
        items=result.items,
    )
