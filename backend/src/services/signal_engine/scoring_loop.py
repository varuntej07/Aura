"""
Scoring loop — runs every 15 minutes via Cloud Scheduler.

For each active user:
  1. Read state and the user's timezone.
  2. Periodic Aura refresh if the user has been idle for >= AURA_REFRESH_INTERVAL_DAYS.
  3. Pull top-K nearest candidates from the content pool.
  4. Score each candidate to a BASE score that EXCLUDES diversity
     (cosine * time_slot * freshness * (1 - fatigue)), via scoring.py.
  5. Among candidates whose base score clears the threshold, use the diversity
     penalty only as a tie-breaker (rank by base * diversity) to prefer a fresh
     category. Diversity never lowers the gate and can never block a send — that
     was the self-reinforcing deadlock fixed on 2026-06-09 (see lessons-learnt.text).
  6. Exploration drift: if the user hasn't engaged in EXPLORATION_DRIFT_THRESHOLD
     ticks, swap in the strongest (highest base) candidate from their
     least-explored category.
  7. If the chosen candidate's score clears the threshold AND the daily cap allows,
     call the LLM framer (10s timeout) once and dispatch FCM.
  8. Record a pending outcome row, update sends_today.
  9. Sweep stale "pending" outcomes older than 6h to "timeout" and apply
     a small negative signal.

All users run concurrently under a Semaphore(10) cap. Errors per user are
isolated; a single user blow-up never stops the loop.
"""

from __future__ import annotations

import asyncio
import statistics
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.cloud.firestore_v1.base_query import FieldFilter

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from ..model_provider import ModelProvider, get_model_provider
from ..notification_budget import try_claim_proactive_slot
from ..user_aura_schema import (
    GENDER_FIELD,
    LANGUAGE_FIELD,
    LOCALE_FIELD,
    ONBOARDING_INTERESTS_FIELD,
    active_category_slugs,
    top_interest_subjects,
)
from ..analytics import posthog_client
from ..analytics.funnel_events import (
    EVENT_NOTIFICATION_SENT,
    NOTIFICATION_ORIGIN_SIGNAL_ENGINE,
    PROP_CATEGORY,
    PROP_CONTENT_ID,
    PROP_NOTIFICATION_ID,
    PROP_NOTIFICATION_ORIGIN,
)
from ..notification_service import send_notification
from . import event_ingester, feature_store
from .content_category_map import POOL_PRODUCIBLE_TAXONOMY_SLUGS, to_taxonomy_slug
from .content_pool import (
    ScoredCandidate,
    find_nearest_for_user,
    has_any_candidate,
    list_recent_breaking_candidates,
)
from .notification_framer import (
    FRAMER_UNAVAILABLE_REASON,
    UserFramingContext,
    _safe_fallback,
    derive_local_time_band,
    frame_notification,
)
from .scoring import (
    BREAKING_SALIENCE_BAR,
    DAILY_NOTIFICATION_HARD_CAP,
    MAX_BREAKING_SENDS_PER_DAY,
    NOTIFICATION_SCORE_THRESHOLD,
    apply_salience_nudge,
    combine_notification_score,
    cosine_similarity,
    diversity_penalty,
    fatigue_penalty,
    freshness_decay,
    is_sendable,
    is_within_active_hours,
    time_slot_open_score,
)
from ..fcm_token_registry import any_token_registered


# How far back we look at outcomes to compute the diversity penalty.
RECENT_OUTCOMES_FOR_DIVERSITY = 5

# Only sends from the last this-many hours count toward the diversity penalty.
# Older outcomes (including timed-out ones) must not keep suppressing a category
# forever - that, combined with diversity being applied as a send gate, created a
# self-reinforcing deadlock that blocked every notification after the first send
# per user (see lessons-learnt.text, 2026-06-09).
DIVERSITY_LOOKBACK_HOURS = 24

# Stale pending-outcome threshold.
OUTCOME_TIMEOUT_HOURS = 6

# Consecutive ticks with no engagement before exploration drift activates.
EXPLORATION_DRIFT_THRESHOLD = 20

# Re-blend the user_vector from UserAura after this many days of inactivity.
AURA_REFRESH_INTERVAL_DAYS = 3

# Category affinity above which a learned affinity contributes to the allow-list.
# 0.5 is the neutral starting affinity (see event_ingester), so only categories
# the user has positively engaged with cross the bar.
ALLOW_SET_AFFINITY_THRESHOLD = 0.5

# Soft region preference. A candidate from the user's own locale edition is gently
# preferred; a foreign-region candidate is gently softened. NEVER a hard filter —
# a global story still reaches everyone — so a wrong/absent locale can't mute.
REGION_MATCH_BOOST = 1.15
REGION_MISMATCH_PENALTY = 0.9

# Max users processed simultaneously in one tick.
TICK_USER_CONCURRENCY = 10


@dataclass
class TickSummary:
    users_considered: int = 0
    users_skipped_no_state: int = 0
    notifications_sent: int = 0
    blocked_below_threshold: int = 0
    blocked_daily_cap: int = 0
    blocked_quiet_hours: int = 0
    timeouts_swept: int = 0
    # Best base score of each below-threshold user, so the tick health line can
    # report how close the pool came (weak matches vs a mis-tuned threshold).
    blocked_below_threshold_scores: list[float] = field(default_factory=list)


async def run_tick() -> TickSummary:
    """Public entrypoint called from the /internal/signal-engine/tick handler."""
    summary = TickSummary()
    user_ids = await feature_store.list_active_user_ids()
    summary.users_considered = len(user_ids)
    
    if not user_ids:
        # Fail loud: 0 active users while tokens exist means the active-user
        # query is misconfigured. A plain "no users" is expected only when truly empty.
        if await asyncio.to_thread(any_token_registered):
            logger.warn(
                "signal_engine.scoring_loop: 0 active users found but FCM tokens exist.. "
                "active-user query likely misconfigured (check fcm_tokens.registered_at)"
            )
        else:
            logger.info("signal_engine.scoring_loop: no active users")
        return summary

    models = get_model_provider()
    semaphore = asyncio.Semaphore(TICK_USER_CONCURRENCY)

    async def _score_with_semaphore(user_id: str) -> None:
        async with semaphore:
            try:
                await _score_one_user(user_id, models, summary)
            except Exception as exc:
                logger.exception("signal_engine.scoring_loop: per-user failure while scoring concurrently using semaphore", {
                    "user_id": user_id,
                    "error": str(exc),
                })

    await asyncio.gather(*[_score_with_semaphore(uid) for uid in user_ids])

    logger.info("signal_engine.scoring_loop: tick complete", {
        "users_considered": summary.users_considered,
        "users_skipped_no_state": summary.users_skipped_no_state,
        "notifications_sent": summary.notifications_sent,
        "blocked_below_threshold": summary.blocked_below_threshold,
        "blocked_daily_cap": summary.blocked_daily_cap,
        "blocked_quiet_hours": summary.blocked_quiet_hours,
        "timeouts_swept": summary.timeouts_swept,
    })

    # One self-explanatory health line so the collapsed log view tells the whole
    # story of a tick without expanding any jsonPayload. The median below-threshold
    # score says HOW close the pool came: ~0.4 means matches are strong and the
    # threshold may be the lever; very low means the content/vector match is weak.
    below_scores = summary.blocked_below_threshold_scores
    median_below = round(statistics.median(below_scores), 3) if below_scores else None
    logger.info(
        f"signal_engine.scoring_loop: tick health — "
        f"sent={summary.notifications_sent}/{summary.users_considered} considered | "
        f"blocked: below_threshold={summary.blocked_below_threshold}"
        f"(median_score={median_below}, threshold={NOTIFICATION_SCORE_THRESHOLD}), "
        f"daily_cap={summary.blocked_daily_cap}, "
        f"quiet_hours={summary.blocked_quiet_hours}, "
        f"no_state={summary.users_skipped_no_state} | "
        f"timeouts_swept={summary.timeouts_swept}",
        {
            "notifications_sent": summary.notifications_sent,
            "users_considered": summary.users_considered,
            "median_below_threshold_score": median_below,
            "notification_score_threshold": NOTIFICATION_SCORE_THRESHOLD,
        },
    )

    # Fail loud: 0 notifications while the content pool is empty means ingest is
    # starved (e.g. Gemini credits exhausted) — distinct from "pool has content but
    # nothing cleared threshold", which is normal. Mirrors the 0-active-users guard.
    if summary.notifications_sent == 0 and summary.users_considered > 0:
        if not await has_any_candidate():
            logger.warn(
                "signal_engine.scoring_loop: 0 notifications and content pool is EMPTY — "
                "ingest is failing to refresh candidates (check Gemini billing / the "
                "content-ingest job). Notifications cannot send with an empty pool.",
                {"users_considered": summary.users_considered},
            )
        elif (
            summary.blocked_below_threshold == 0
            and summary.blocked_daily_cap == 0
            and summary.blocked_quiet_hours == 0
        ):
            # Pool has content and nobody was even scored — every user fell out
            # before the threshold gate. Most likely the vector index is missing
            # (find_nearest returns [] silently) or no user has a bootstrapped
            # vector. Distinct from the normal "scored but nothing cleared 0.45".
            logger.warn(
                "signal_engine.scoring_loop: 0 notifications but pool has content AND "
                "no user reached the scoring gate — vector search likely failing "
                "(missing content_candidates.embedding index) or no user has a vector. "
                "Check the find_nearest_for_user error log.",
                {
                    "users_considered": summary.users_considered,
                    "users_skipped_no_state": summary.users_skipped_no_state,
                },
            )

    # Fail loud: sending notifications while analytics is unconfigured means the
    # re-engagement funnel is silently blind to every send (the "zero rows looks
    # healthy" trap). A plain absence of the key is only expected in dev.
    if summary.notifications_sent > 0 and not settings.posthog_configured:
        logger.warn(
            "signal_engine.scoring_loop: sent notifications but POSTHOG_API_KEY "
            "is unset — re-engagement funnel is blind to these sends",
            {"notifications_sent": summary.notifications_sent},
        )

    # Funnel events were captured fire-and-forget onto PostHog's background queue.
    # Cloud Run freezes this container the moment the tick returns, so flush the
    # queue out to the server first or step-1 sends are silently lost.
    await posthog_client.flush()

    return summary


async def _score_one_user(
    user_id: str,
    models: ModelProvider,
    summary: TickSummary,
) -> None:
    state = await feature_store.read_state(user_id)

    # One read of users/{uid} gives timezone (scheduling), locale (region
    # preference + framer language), language (framer output), gender (framer
    # tone), and the declared onboarding interests (allow-list).
    try:
        user_doc = await asyncio.wait_for(_load_user_doc(user_id), timeout=5.0)
    except TimeoutError:
        user_doc = {}
    user_timezone = str(user_doc.get("timezone", "UTC") or "UTC")

    user_local_now = _local_now(user_timezone)
    user_local_date = user_local_now.date().isoformat()

    # Reset the daily counters when the calendar day has flipped.
    if state.sends_today_date != user_local_date:
        state.sends_today = 0
        state.breaking_sends_today = 0
        state.sends_today_date = user_local_date

    timeouts = await _sweep_timeouts(user_id)
    summary.timeouts_swept += timeouts
    if timeouts > 0:
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + timeouts)

    # Hard quiet-hours gate FIRST: never deliver at night, on EITHER lane. Moved
    # ahead of the personal-signal check so the vector-independent breaking lane is
    # also night-gated. Skipped without bumping the no-open counter so nighttime
    # ticks don't trigger exploration drift.
    if not is_within_active_hours(user_local_now.hour, user_local_now.minute):
        await _safe_write_state(user_id, state)
        summary.blocked_quiet_hours += 1
        return

    # Content already sent to this user. Both lanes drop these so the same story is
    # never re-sent — when the top pick equals a previous send, the next-best fresh
    # one is chosen instead.
    sent_content_ids = await _load_recent_sent_content_ids(user_id)

    # LANE B — breaking news. Vector-INDEPENDENT, so it runs BEFORE the personal
    # bootstrap / has-signal gate: a genuinely worldwide story reaches even a
    # brand-new user with no interest vector yet (the icebreaker owns the rest of
    # their cold start). Hard-capped at MAX_BREAKING_SENDS_PER_DAY, counted in
    # sends_today, and spaced/capped by the unified budget like any proactive send.
    if (
        state.breaking_sends_today < MAX_BREAKING_SENDS_PER_DAY
        and state.sends_today < DAILY_NOTIFICATION_HARD_CAP
    ):
        sent_breaking = await _try_send_breaking(
            user_id, models, state, user_doc, sent_content_ids,
            user_local_now, user_local_date, summary,
        )
        if sent_breaking:
            await _safe_write_state(user_id, state)
            return

    # Cold start: a user with no vector yet is bootstrapped from their UserAura
    # interests here, on the loop itself, instead of waiting for a /events call
    # that may never arrive. Without this the loop and the client deadlock:
    # no vector -> skipped -> no notification -> no tap -> no event -> no vector.
    if not state.bootstrap_done:
        state = await event_ingester.bootstrap_user_vector(user_id, state)

    # Still no signal after bootstrap (no UserAura profile / consent not granted):
    # the personal lane has nothing to match on, so skip without spending on
    # scoring. (Breaking was already tried above and is vector-independent.)
    if not _has_any_signal(state):
        await _safe_write_state(user_id, state)
        summary.users_skipped_no_state += 1
        return

    # Periodic Aura refresh for idle users so evolved interests surface.
    if _should_refresh_user_vector(state):
        state = await event_ingester.refresh_user_vector_from_aura(user_id, state)

    candidates = await find_nearest_for_user(state.user_vector, limit=50)
    if not candidates:
        await _safe_write_state(user_id, state)
        return

    recent_categories = await _load_recent_outcome_categories(user_id)

    # Build the category allow-list (Layer 2). Read UserAura once here so it feeds
    # both the allow-list and, on a send, the framing context (no second read).
    aura = await _read_user_aura(user_id)
    allow_slugs, effective = _build_category_allow_set(aura, user_doc, state)
    gate_a_active = bool(effective)
    
    if not gate_a_active:
        # The user's interests are real but NO source can satisfy them. Do NOT
        # mute, skip Gate A and lean on Gate B (the framer's relevance confirm).
        logger.warn(
            "signal_engine.scoring_loop: allow_set has no producible category "
            "Gate A skipped (no-blackout safeguard), relying on relevance confirm",
            {
                "user_id": user_id,
                "allow_set_size": len(allow_slugs),
                "producible": sorted(POOL_PRODUCIBLE_TAXONOMY_SLUGS),
            },
        )

    # Soft region preference: a candidate from the user's own locale edition is
    # gently boosted, a foreign one gently softened. Never a hard filter.
    user_region = _region_from_locale(str(user_doc.get(LOCALE_FIELD, "") or ""))

    # Each entry is (base_score, diversity, candidate, components). The base score
    # deliberately EXCLUDES diversity: sendability is decided on base alone, and
    # diversity only orders the choice among already-sendable candidates. Folding
    # diversity into the gating score let a single recent same-category send drop
    # every future score under the threshold permanently (deadlock, 2026-06-09).
    scored: list[tuple[float, float, ScoredCandidate, dict[str, float]]] = []
    now_utc = datetime.now(UTC)
    for cand in candidates:
        if not cand.embedding:
            continue
        # Push-ineligible items (e.g. bodyless, blanket-tagged stories) may rank in
        # the feed but must never fire a notification — drop them from the
        # notification candidate set before scoring. The feed path is separate.
        if not cand.push_eligible:
            continue
        # Already-sent suppression: never re-send the same story on the personal lane.
        if cand.content_id in sent_content_ids:
            continue
        cosine = cand.cosine_similarity or cosine_similarity(state.user_vector, cand.embedding)
        slot = time_slot_open_score(
            state.time_slot_open_rates,
            user_local_hour=user_local_now.hour,
            user_local_minute=user_local_now.minute,
        )
        fresh = freshness_decay(cand.freshness_ts, now=now_utc)
        fat = fatigue_penalty(state.sends_today, state.last_notification_at, now=now_utc)
        div = diversity_penalty(cand.category, recent_categories)
        region_mult = _region_multiplier(user_region, cand.region)
        base = combine_notification_score(
            cosine=cosine, time_slot=slot, freshness=fresh, fatigue=fat, diversity=1.0,
        )
        base = min(2.0, base * region_mult)
        # Mild salience nudge: a more globally-important story edges ahead among
        # candidates that already clear the threshold on personal relevance. Never
        # lowers a score and never gates — the breaking lane handles bypass sends.
        base = apply_salience_nudge(base, cand.salience)

        scored.append((base, div, cand, {
            "cosine": cosine, "slot": slot, "freshness": fresh,
            "fatigue": fat, "diversity": div, "region": region_mult,
            "salience": cand.salience,
        }))

    if not scored:
        await _safe_write_state(user_id, state)
        return

    def _in_allow_set(cand: ScoredCandidate) -> bool:
        # Normalise the candidate category through the map so pre-deploy pool docs
        # still on the old vocab match too (idempotent on taxonomy slugs).
        return to_taxonomy_slug(cand.category) in allow_slugs

    # Candidates that clear the bar on their own merit. Among these, diversity is a
    # tie-breaker (base * diversity) that prefers a fresh category — it can no
    # longer block a send, only influence which sendable item is picked. Gate A
    # restricts the normal pick to in-allow-set candidates; when Gate A is skipped
    # (no producible interest) the whole threshold set is eligible.
    threshold_clearers = [item for item in scored if item[0] >= NOTIFICATION_SCORE_THRESHOLD]
    if gate_a_active:
        eligible = [item for item in threshold_clearers if _in_allow_set(item[2])]
    else:
        eligible = threshold_clearers

    if eligible:
        best_score, _, best_cand, components = max(
            eligible, key=lambda item: item[0] * item[1]
        )
        have_pick = True
    else:
        # Either nothing cleared the threshold, or nothing in-interest did. Keep the
        # strongest overall match for logging; do NOT send it (Gate A would be
        # violated). have_pick=False routes to the below-threshold block path.
        best_score, _, best_cand, components = max(scored, key=lambda item: item[0])
        have_pick = False

    # Exploration drift: the ONE exit that reaches outside the allow-list. After
    # enough consecutive missed ticks, swap in a low-affinity candidate (often
    # out-of-allow-set) to break the user out of a content rut. Gate B (the
    # framer's is_relevant) is the confirm that keeps an off-base item from sending.
    if state.consecutive_no_open_ticks >= EXPLORATION_DRIFT_THRESHOLD:
        exploration = _find_exploration_candidate(scored, state)
        if (
            exploration is not None
            and exploration[2].category != best_cand.category
            and exploration[0] >= NOTIFICATION_SCORE_THRESHOLD
        ):
            best_score, _, best_cand, components = exploration
            have_pick = True
            logger.info("signal_engine.scoring_loop: applying exploration drift", {
                "user_id": user_id,
                "exploration_category": best_cand.category,
                "consecutive_no_open_ticks": state.consecutive_no_open_ticks,
            })

    if not have_pick:
        # No sendable candidate (below threshold, or no in-interest match this
        # tick). Recover next tick as the pool refreshes — never a permanent mute.
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
        await _safe_write_state(user_id, state)
        summary.blocked_below_threshold += 1
        summary.blocked_below_threshold_scores.append(best_score)
        logger.info(
            f"signal_engine.scoring_loop: not sending "
            f"(reason=below_threshold_or_gate_a, score={round(best_score, 3)}, "
            f"threshold={NOTIFICATION_SCORE_THRESHOLD}, gate_a={gate_a_active})",
            {
                "user_id": user_id,
                "best_score": round(best_score, 3),
                "gate_a_active": gate_a_active,
                "components": {k: round(v, 3) for k, v in components.items()},
            },
        )
        return

    allowed, block_reason = is_sendable(
        best_score,
        state.sends_today,
        state.sends_today_date,
        user_local_date,
    )
    if not allowed:
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
        await _safe_write_state(user_id, state)
        if block_reason == "daily_hard_cap":
            summary.blocked_daily_cap += 1
        else:
            summary.blocked_below_threshold += 1
            summary.blocked_below_threshold_scores.append(best_score)
        # Fold the decisive facts into the message itself so the collapsed Cloud
        # Logging one-line view is readable without expanding jsonPayload.
        logger.info(
            f"signal_engine.scoring_loop: not sending "
            f"(reason={block_reason}, score={round(best_score, 3)}, "
            f"threshold={NOTIFICATION_SCORE_THRESHOLD}, "
            f"diversity={round(components.get('diversity', 1.0), 2)})",
            {
                "user_id": user_id,
                "best_score": round(best_score, 3),
                "reason": block_reason,
                "components": {k: round(v, 3) for k, v in components.items()},
            },
        )
        return

    # Coordinated ceiling across all proactive deciders (no-op while the flag is
    # off). Claimed before the LLM framing call so a budget-blocked tick spends
    # nothing. The engine's own DAILY_HARD_CAP above remains the per-source cap.
    budget = await try_claim_proactive_slot(
        user_id, source="signal_engine", user_local_date=user_local_date,
    )
    if not budget.allowed:
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
        await _safe_write_state(user_id, state)
        summary.blocked_daily_cap += 1
        logger.info(
            f"signal_engine.scoring_loop: not sending (global budget: {budget.reason})",
            {"user_id": user_id, "reason": budget.reason},
        )
        return

    # Build the framing context from the UserAura already read for the allow-set,
    # plus gender/language from the user doc (tone + output language). No new read.
    user_context = _build_framing_context(aura, user_doc, user_local_now)

    try:
        framed = await asyncio.wait_for(
            frame_notification(models, best_cand, user_context), timeout=10.0
        )
    except TimeoutError:
        logger.warn("signal_engine.scoring_loop: framer LLM timed out, using fallback", {
            "user_id": user_id,
            "content_id": best_cand.content_id,
        })
        framed = _safe_fallback(best_cand)

    # Gate B — the relevance contract, fail-CLOSED on a missing reason. A send fires
    # only when the framer affirmed relevance AND named the specific interest it
    # matches (relevance_reason). An is_relevant=false, an empty reason, or the
    # framer-unavailable sentinel all suppress the send — so every notification that
    # DOES fire carries a defensible, recorded reason.
    relevance_reason = (framed.relevance_reason or "").strip()
    if framed.relevance_reason == FRAMER_UNAVAILABLE_REASON:
        # Infra outage, NOT a content rejection. Defer this tick and scream so a
        # sustained framer outage never looks like "nothing was relevant" (fail-loud).
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
        await _safe_write_state(user_id, state)
        summary.blocked_below_threshold += 1
        logger.warn(
            "signal_engine.scoring_loop: not sending — framer UNAVAILABLE "
            "(deferring this tick, infra not relevance; retries next tick)",
            {
                "user_id": user_id,
                "content_id": best_cand.content_id,
                "category": best_cand.category,
            },
        )
        return
    if not framed.is_relevant or not relevance_reason:
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
        await _safe_write_state(user_id, state)
        summary.blocked_below_threshold += 1
        logger.info(
            "signal_engine.scoring_loop: not sending (relevance gate: "
            f"{'no reason given' if framed.is_relevant else 'not relevant'})",
            {
                "user_id": user_id,
                "content_id": best_cand.content_id,
                "category": best_cand.category,
                "relevance_reason": framed.relevance_reason,
            },
        )
        return

    notification_id = str(uuid.uuid4())
    sent_at = datetime.now(UTC)
    result = await send_notification(
        user_id,
        title=framed.title,
        body=framed.body,
        data={
            "deep_link": "chat",
            "content_id": best_cand.content_id,
            "notification_id": notification_id,
            "category": best_cand.category,
            "sub_category": best_cand.sub_category,
            "source": best_cand.source,
            "url": best_cand.url,
            "content_kind": framed.content_kind,
            "opening_chat_message": framed.opening_chat_message,
            "notification_origin": "signal_engine",
        },
        notification_type="signal_engine",
        collapse_key=f"signal_{notification_id}",
    )

    if not result.delivered:
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
        await _safe_write_state(user_id, state)
        logger.info("signal_engine.scoring_loop: send returned no delivery", {
            "user_id": user_id,
            "notification_id": notification_id,
        })
        return

    state.sends_today += 1
    state.last_notification_at = sent_at
    await _safe_write_state(user_id, state)
    await feature_store.write_outcome_pending(
        user_id,
        notification_id,
        content_id=best_cand.content_id,
        score=best_score,
        scored_at=now_utc,
        sent_at=sent_at,
        relevance_reason=relevance_reason,
    )
    summary.notifications_sent += 1

    # Top of the re-engagement funnel. Fire-and-forget; never blocks the tick.
    # The shared property keys must match the client's tap event for PostHog to
    # join sent -> tapped -> session -> action (see analytics/funnel_events.py).
    await posthog_client.capture_event(
        distinct_id=user_id,
        event=EVENT_NOTIFICATION_SENT,
        properties={
            PROP_NOTIFICATION_ID: notification_id,
            PROP_CONTENT_ID: best_cand.content_id,
            PROP_CATEGORY: best_cand.category,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_SIGNAL_ENGINE,
            "sub_category": best_cand.sub_category,
            "source": best_cand.source,
            "score": round(best_score, 4),
            "relevance_reason": relevance_reason,
        },
    )

    logger.info(
        f"signal_engine.scoring_loop: notification sent "
        f"(category={best_cand.category}, score={round(best_score, 3)}, "
        f"reason={relevance_reason!r})",
        {
            "user_id": user_id,
            "notification_id": notification_id,
            "content_id": best_cand.content_id,
            "category": best_cand.category,
            "sub_category": best_cand.sub_category,
            "best_score": round(best_score, 3),
            "relevance_reason": relevance_reason,
            "components": {k: round(v, 3) for k, v in components.items()},
        },
    )


async def _safe_write_state(user_id: str, state: feature_store.SignalStoreState) -> None:
    """Write state, swallowing failures after feature_store already logged them."""
    try:
        await feature_store.write_state(user_id, state)
    except Exception:
        pass


def _has_any_signal(state: feature_store.SignalStoreState) -> bool:
    return any(abs(x) > 1e-9 for x in state.user_vector)


def _should_refresh_user_vector(state: feature_store.SignalStoreState) -> bool:
    """True when it's been >= AURA_REFRESH_INTERVAL_DAYS since the last bootstrap."""
    if not state.bootstrap_done:
        return False
    if state.last_bootstrap_at is None:
        # Pre-existing user before last_bootstrap_at was introduced — refresh now.
        return True
    age_days = (datetime.now(UTC) - state.last_bootstrap_at).total_seconds() / 86400
    return age_days >= AURA_REFRESH_INTERVAL_DAYS


def _find_exploration_candidate(
    scored: list[tuple[float, float, ScoredCandidate, dict[str, float]]],
    state: feature_store.SignalStoreState,
) -> tuple[float, float, ScoredCandidate, dict[str, float]] | None:
    """Return the strongest candidate from the category with the lowest affinity.

    Adds a flat +0.15 exploration bonus to its base score. Returns None if all
    categories have the same affinity or there are no scored candidates. Tuple
    shape matches the scored list: (base_score, diversity, candidate, components).

    The "strongest" candidate is the one with the highest base score in the target
    category. This must NOT depend on the order of `scored` — the caller no longer
    pre-sorts that list by score (sendability is decided by a sendable/max split),
    so picking the first match in iteration order would silently send a weaker
    story than the best available in that category.
    """
    if not scored:
        return None
    all_categories = {cand.category for _, _, cand, _ in scored if cand.category}
    if not all_categories:
        return None

    def category_affinity(cat: str) -> float:
        return state.category_affinities.get(cat, 0.0)

    target_category = min(all_categories, key=category_affinity)

    in_target_category = [item for item in scored if item[2].category == target_category]
    if not in_target_category:
        return None
    base, div, cand, comps = max(in_target_category, key=lambda item: item[0])
    boosted_score = min(2.0, base + 0.15)
    return (boosted_score, div, cand, {**comps, "exploration_bonus": 0.15})


async def _load_user_doc(user_id: str) -> dict[str, Any]:
    """Read users/{uid} once. Returns {} on miss/error so every reader falls back
    to its own safe default (UTC timezone, neutral region, English, no declared
    interests) — a missing doc never mutes or crashes a tick."""
    def _fetch() -> dict[str, Any]:
        doc = admin_firestore().collection("users").document(user_id).get()
        if doc.exists:
            return doc.to_dict() or {}
        return {}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return {}


async def _read_user_aura(user_id: str) -> dict[str, Any]:
    """Read UserAura/{uid} once per tick. Returns {} on miss/error."""
    def _fetch() -> dict[str, Any]:
        snap = admin_firestore().collection("UserAura").document(user_id).get()
        if not snap.exists:
            return {}
        return snap.to_dict() or {}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("signal_engine.scoring_loop: UserAura read failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return {}


def _build_category_allow_set(
    aura: dict[str, Any],
    user_doc: dict[str, Any],
    state: feature_store.SignalStoreState,
    now: datetime | None = None,
) -> tuple[set[str], set[str]]:
    """Build (allow_slugs, effective) for Gate A.

    allow_slugs = taxonomy-slug union of:
      - live (decayed weight>0) UserAura interest categories,
      - declared onboarding_interests,
      - learned category affinities above the neutral bar (mapped through the
        source->taxonomy map so a legacy pool-vocab affinity key like "tech"
        contributes "technology_computing").
    effective = allow_slugs ∩ POOL_PRODUCIBLE_TAXONOMY_SLUGS — the predicate that
    actually decides whether Gate A can run. A non-empty allow_slugs whose
    producible intersection is EMPTY means the user's interests are real but no
    source can satisfy them; the caller then skips Gate A (no-blackout safeguard).
    """
    now = now or datetime.now(UTC)
    allow: set[str] = set(active_category_slugs(aura, now))

    declared = user_doc.get(ONBOARDING_INTERESTS_FIELD)
    if isinstance(declared, list):
        allow.update(str(s) for s in declared if s)

    for raw_cat, affinity in state.category_affinities.items():
        try:
            if float(affinity) > ALLOW_SET_AFFINITY_THRESHOLD:
                allow.add(to_taxonomy_slug(raw_cat))
        except (TypeError, ValueError):
            continue

    effective = allow & POOL_PRODUCIBLE_TAXONOMY_SLUGS
    return allow, effective


def _region_from_locale(locale: str) -> str:
    """Extract an uppercased region code from a stored locale.

    Accepts a bare country code ("IN"), a hyphen/underscore locale ("en-IN",
    "en_IN"), and returns "" for anything empty so the region preference stays
    neutral when no locale was captured (e.g. existing beta users)."""
    value = (locale or "").strip()
    if not value:
        return ""
    for sep in ("-", "_"):
        if sep in value:
            return value.split(sep)[-1].upper()
    return value.upper()


def _region_multiplier(user_region: str, candidate_region: str) -> float:
    """Soft region preference multiplier. Neutral (1.0) when either side is
    unknown, so region-agnostic content (HN, arXiv) and locale-less users are
    never penalised — region is a nudge, never a gate."""
    if not user_region or not candidate_region:
        return 1.0
    return REGION_MATCH_BOOST if candidate_region.upper() == user_region else REGION_MISMATCH_PENALTY


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


async def _load_recent_outcome_categories(user_id: str) -> list[str]:
    """Categories from sends in the last DIVERSITY_LOOKBACK_HOURS, most-recent
    first. Outcomes older than the window are ignored so a stale send can't keep
    suppressing its category forever. Parallel fetches."""
    cutoff = datetime.now(UTC) - timedelta(hours=DIVERSITY_LOOKBACK_HOURS)

    def _fetch_outcome_content_ids() -> list[str]:
        db = admin_firestore()
        snaps = (
            db.collection("users").document(user_id)
            .collection("signal_store").document("state")
            .collection("outcomes")
            .order_by("sent_at", direction="DESCENDING")
            .limit(RECENT_OUTCOMES_FOR_DIVERSITY)
            .stream()
        )
        recent: list[str] = []
        for s in snaps:
            data = s.to_dict() or {}
            sent_at = data.get("sent_at")
            # Drop sends older than the lookback window. A tz-naive timestamp is
            # treated as UTC to match how write_outcome_pending stores it.
            if isinstance(sent_at, datetime):
                if sent_at.tzinfo is None:
                    sent_at = sent_at.replace(tzinfo=UTC)
                if sent_at < cutoff:
                    continue
            recent.append(str(data.get("content_id", "")))
        return recent

    try:
        content_ids = await asyncio.to_thread(_fetch_outcome_content_ids)
    except Exception as exc:
        logger.warn("signal_engine.scoring_loop: outcome history fetch failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return []

    valid_ids = [cid for cid in content_ids if cid]
    if not valid_ids:
        return []

    from .content_pool import get_candidate
    results = await asyncio.gather(*[get_candidate(cid) for cid in valid_ids])
    return [cand.category for cand in results if cand and cand.category]


async def _load_recent_sent_content_ids(user_id: str, limit: int = 60) -> set[str]:
    """content_ids of the user's recent sends (any outcome), for already-sent
    suppression on both lanes. Orders by sent_at desc — a single-field order
    Firestore auto-indexes at collection scope, so no composite index is needed.
    News candidates expire from the pool within ~24h, so a 60-row window more than
    covers any item still selectable. Returns an empty set on error (fail-open:
    suppression is a nicety, never a reason to mute)."""
    def _fetch() -> set[str]:
        db = admin_firestore()
        snaps = (
            db.collection("users").document(user_id)
            .collection("signal_store").document("state")
            .collection("outcomes")
            .order_by("sent_at", direction="DESCENDING")
            .limit(limit)
            .stream()
        )
        return {str((s.to_dict() or {}).get("content_id", "")) for s in snaps} - {""}

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("signal_engine.scoring_loop: recent-sent fetch failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return set()


async def _try_send_breaking(
    user_id: str,
    models: ModelProvider,
    state: feature_store.SignalStoreState,
    user_doc: dict[str, Any],
    sent_content_ids: set[str],
    user_local_now: datetime,
    user_local_date: str,
    summary: TickSummary,
) -> bool:
    """Lane B: try to send ONE globally-significant breaking story, bypassing the
    personal interest gate. Returns True iff a notification was delivered (the
    caller then persists state). Vector-independent — reads the freshest high-
    salience pool items directly, not the user's vector neighbours.

    Mutates state (sends_today, breaking_sends_today, last_notification_at) on a
    successful send; the caller writes state. Every other branch leaves state for
    the personal lane to continue."""
    candidates = await list_recent_breaking_candidates(
        min_salience=BREAKING_SALIENCE_BAR, limit=40,
    )
    pick = next((c for c in candidates if c.content_id not in sent_content_ids), None)
    if pick is None:
        return False

    # Coordinated proactive ceiling + spacing. Claimed before the LLM framing call
    # so a budget-blocked breaking tick spends nothing.
    budget = await try_claim_proactive_slot(
        user_id, source="signal_engine_breaking", user_local_date=user_local_date,
    )
    if not budget.allowed:
        logger.info(
            f"signal_engine.scoring_loop: breaking not sent (global budget: {budget.reason})",
            {"user_id": user_id, "content_id": pick.content_id, "reason": budget.reason},
        )
        return False

    aura = await _read_user_aura(user_id)
    user_context = _build_framing_context(aura, user_doc, user_local_now)
    try:
        framed = await asyncio.wait_for(
            frame_notification(models, pick, user_context, breaking_news=True), timeout=10.0
        )
    except TimeoutError:
        framed = _safe_fallback(pick)

    # A framer outage is infra, not a content rejection — defer this tick (the
    # personal lane will also find the framer down, so just stop here).
    if framed.relevance_reason == FRAMER_UNAVAILABLE_REASON:
        logger.warn(
            "signal_engine.scoring_loop: breaking framer UNAVAILABLE, deferring this tick",
            {"user_id": user_id, "content_id": pick.content_id},
        )
        return False

    notification_id = str(uuid.uuid4())
    sent_at = datetime.now(UTC)
    result = await send_notification(
        user_id,
        title=framed.title,
        body=framed.body,
        data={
            "deep_link": "chat",
            "content_id": pick.content_id,
            "notification_id": notification_id,
            "category": pick.category,
            "sub_category": pick.sub_category,
            "source": pick.source,
            "url": pick.url,
            "content_kind": framed.content_kind,
            "opening_chat_message": framed.opening_chat_message,
            "notification_origin": "signal_engine",
            # Breaking reuses the signal_engine origin so the existing client tap
            # routing + funnel join work unchanged; `lane` only separates analytics.
            "lane": "breaking",
        },
        notification_type="signal_engine",
        collapse_key=f"signal_{notification_id}",
    )
    if not result.delivered:
        logger.info("signal_engine.scoring_loop: breaking send returned no delivery", {
            "user_id": user_id,
            "notification_id": notification_id,
        })
        return False

    state.sends_today += 1
    state.breaking_sends_today += 1
    state.last_notification_at = sent_at
    await feature_store.write_outcome_pending(
        user_id,
        notification_id,
        content_id=pick.content_id,
        score=pick.salience,
        scored_at=sent_at,
        sent_at=sent_at,
        relevance_reason=framed.relevance_reason or "globally significant breaking news",
    )
    summary.notifications_sent += 1

    await posthog_client.capture_event(
        distinct_id=user_id,
        event=EVENT_NOTIFICATION_SENT,
        properties={
            PROP_NOTIFICATION_ID: notification_id,
            PROP_CONTENT_ID: pick.content_id,
            PROP_CATEGORY: pick.category,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_SIGNAL_ENGINE,
            "sub_category": pick.sub_category,
            "source": pick.source,
            "lane": "breaking",
            "salience": round(pick.salience, 4),
        },
    )

    logger.info(
        f"signal_engine.scoring_loop: BREAKING notification sent "
        f"(category={pick.category}, salience={round(pick.salience, 3)})",
        {
            "user_id": user_id,
            "notification_id": notification_id,
            "content_id": pick.content_id,
            "category": pick.category,
            "salience": round(pick.salience, 3),
        },
    )
    return True


def _build_framing_context(
    aura: dict[str, Any],
    user_doc: dict[str, Any],
    user_local_now: datetime,
) -> UserFramingContext:
    """Assemble the framer's read-only view from the already-read UserAura + user
    doc. Pure (no I/O): both docs are read once in _score_one_user and passed in.

    gender (tone only) and language (output language) come from the user doc;
    interests + tone + depth come from UserAura. language defaults to English.
    """
    # Specific subjects (e.g. "KCR", "XUV 3XO") give the framer a concrete hook to
    # personalise copy; falls back to legacy free-text interests for old profiles.
    top_interests = top_interest_subjects(aura, k=3)
    language = str(user_doc.get(LANGUAGE_FIELD, "") or "").strip() or "English"
    gender = str(user_doc.get(GENDER_FIELD, "") or "").strip() or None

    return UserFramingContext(
        top_interests=top_interests,
        dominant_tone=aura.get("dominant_tone"),
        user_local_time_band=derive_local_time_band(user_local_now),
        depth_level=int(aura.get("emotional_engagement_level", 1) or 1),
        gender=gender,
        language=language,
    )


async def _sweep_timeouts(user_id: str) -> int:
    """Find pending outcomes older than OUTCOME_TIMEOUT_HOURS, flip to timeout,
    and apply a small negative event so the user vector drifts away."""
    cutoff = datetime.now(UTC) - timedelta(hours=OUTCOME_TIMEOUT_HOURS)

    def _fetch_stale() -> list[tuple[str, str]]:
        db = admin_firestore()
        snaps = (
            db.collection("users").document(user_id)
            .collection("signal_store").document("state")
            .collection("outcomes")
            .where(filter=FieldFilter("outcome", "==", "pending"))
            .where(filter=FieldFilter("sent_at", "<", cutoff))
            .limit(20)
            .stream()
        )
        return [(s.id, str((s.to_dict() or {}).get("content_id", ""))) for s in snaps]

    try:
        stale = await asyncio.wait_for(asyncio.to_thread(_fetch_stale), timeout=5.0)
    except (TimeoutError, Exception) as exc:
        logger.warn("signal_engine.scoring_loop: stale-outcome scan failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return 0

    if not stale:
        return 0

    count = 0
    for notif_id, content_id in stale:
        flipped = await feature_store.resolve_outcome(user_id, notif_id, outcome="timeout")
        if flipped is not None:
            count += 1
            if content_id:
                await event_ingester.apply_event(
                    user_id,
                    event_type="notification_timeout",
                    content_id=content_id,
                )
    return count
