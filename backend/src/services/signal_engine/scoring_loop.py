"""
Scoring loop — runs every 15 minutes via Cloud Scheduler.

For each active user:
  1. Read state and the user's timezone.
  2. Periodic Aura refresh if the user has been idle for >= AURA_REFRESH_INTERVAL_DAYS.
  3. Pull top-K nearest candidates from the content pool.
  4. Score each candidate with pure functions in scoring.py.
  5. Apply diversity penalty using recent outcomes.
  6. Exploration drift: if the user hasn't engaged in EXPLORATION_DRIFT_THRESHOLD
     ticks, swap in the best candidate from their least-explored category.
  7. If the best score clears the threshold AND the daily cap allows,
     call the LLM framer (10s timeout) once and dispatch FCM.
  8. Record a pending outcome row, update sends_today.
  9. Sweep stale "pending" outcomes older than 6h to "timeout" and apply
     a small negative signal.

All users run concurrently under a Semaphore(10) cap. Errors per user are
isolated; a single user blow-up never stops the loop.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google.cloud.firestore_v1.base_query import FieldFilter

from ...config.settings import settings
from ...lib.logger import logger
from ..firebase import admin_firestore
from ..model_provider import ModelProvider, get_model_provider
from ..user_aura_schema import top_interest_subjects
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
from .content_pool import ScoredCandidate, find_nearest_for_user, has_any_candidate
from .notification_framer import (
    UserFramingContext,
    _safe_fallback,
    derive_local_time_band,
    frame_notification,
)
from .scoring import (
    NOTIFICATION_SCORE_THRESHOLD,
    combine_notification_score,
    cosine_similarity,
    diversity_penalty,
    fatigue_penalty,
    freshness_decay,
    is_sendable,
    is_within_active_hours,
    time_slot_open_score,
)

# How far back we look at outcomes to compute the diversity penalty.
RECENT_OUTCOMES_FOR_DIVERSITY = 5

# Stale pending-outcome threshold.
OUTCOME_TIMEOUT_HOURS = 6

# Consecutive ticks with no engagement before exploration drift activates.
EXPLORATION_DRIFT_THRESHOLD = 20

# Re-blend the user_vector from UserAura after this many days of inactivity.
AURA_REFRESH_INTERVAL_DAYS = 3

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


async def run_tick() -> TickSummary:
    """Public entrypoint called from the /internal/signal-engine/tick handler."""
    summary = TickSummary()
    user_ids = await feature_store.list_active_user_ids()
    summary.users_considered = len(user_ids)
    if not user_ids:
        # Fail loud: 0 active users while tokens exist means the active-user
        # query is misconfigured. A plain "no users" is expected only when truly empty.
        from ..fcm_token_registry import any_token_registered

        if await asyncio.to_thread(any_token_registered):
            logger.warn(
                "signal_engine.scoring_loop: 0 active users but FCM tokens exist — "
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
                logger.exception("signal_engine.scoring_loop: per-user failure", {
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

    try:
        user_timezone = await asyncio.wait_for(_load_user_timezone(user_id), timeout=5.0)
    except TimeoutError:
        user_timezone = "UTC"

    user_local_now = _local_now(user_timezone)
    user_local_date = user_local_now.date().isoformat()

    # Reset the daily counter when the calendar day has flipped.
    if state.sends_today_date != user_local_date:
        state.sends_today = 0
        state.sends_today_date = user_local_date

    timeouts = await _sweep_timeouts(user_id)
    summary.timeouts_swept += timeouts
    if timeouts > 0:
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + timeouts)

    # Cold start: a user with no vector yet is bootstrapped from their UserAura
    # interests here, on the loop itself, instead of waiting for a /events call
    # that may never arrive. Without this the loop and the client deadlock —
    # no vector -> skipped -> no notification -> no tap -> no event -> no vector.
    if not state.bootstrap_done:
        state = await event_ingester.bootstrap_user_vector(user_id, state)

    # Still no signal after bootstrap (no UserAura profile / consent not granted):
    # nothing to match on, so skip without spending on scoring.
    if not _has_any_signal(state):
        await _safe_write_state(user_id, state)
        summary.users_skipped_no_state += 1
        return

    # Hard quiet-hours gate: never deliver at night regardless of score. Skipped
    # without bumping the no-open counter so nighttime ticks don't trigger
    # exploration drift.
    if not is_within_active_hours(user_local_now.hour):
        await _safe_write_state(user_id, state)
        summary.blocked_quiet_hours += 1
        return

    # Periodic Aura refresh for idle users so evolved interests surface.
    if _should_refresh_user_vector(state):
        state = await event_ingester.refresh_user_vector_from_aura(user_id, state)

    candidates = await find_nearest_for_user(state.user_vector, limit=50)
    if not candidates:
        await _safe_write_state(user_id, state)
        return

    recent_categories = await _load_recent_outcome_categories(user_id)

    scored: list[tuple[float, ScoredCandidate, dict[str, float]]] = []
    now_utc = datetime.now(UTC)
    for cand in candidates:
        if not cand.embedding:
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
        final = combine_notification_score(
            cosine=cosine, time_slot=slot, freshness=fresh, fatigue=fat, diversity=div,
        )
        scored.append((final, cand, {
            "cosine": cosine, "slot": slot, "freshness": fresh,
            "fatigue": fat, "diversity": div,
        }))

    if not scored:
        await _safe_write_state(user_id, state)
        return

    scored.sort(key=lambda item: item[0], reverse=True)
    best_score, best_cand, components = scored[0]

    # Exploration drift: swap in a low-affinity category candidate after enough
    # consecutive missed ticks to break the user out of a content rut.
    if state.consecutive_no_open_ticks >= EXPLORATION_DRIFT_THRESHOLD:
        exploration = _find_exploration_candidate(scored, state)
        if (
            exploration is not None
            and exploration[1].category != best_cand.category
            and exploration[0] >= NOTIFICATION_SCORE_THRESHOLD
        ):
            best_score, best_cand, components = exploration
            logger.info("signal_engine.scoring_loop: applying exploration drift", {
                "user_id": user_id,
                "exploration_category": best_cand.category,
                "consecutive_no_open_ticks": state.consecutive_no_open_ticks,
            })

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
        logger.info("signal_engine.scoring_loop: not sending", {
            "user_id": user_id,
            "best_score": round(best_score, 3),
            "reason": block_reason,
            "components": {k: round(v, 3) for k, v in components.items()},
        })
        return

    try:
        user_context = await asyncio.wait_for(
            _build_framing_context(user_id, user_local_now), timeout=5.0
        )
    except TimeoutError:
        logger.warn("signal_engine.scoring_loop: framing context timed out, using defaults", {
            "user_id": user_id,
        })
        user_context = UserFramingContext(
            user_local_time_band=derive_local_time_band(user_local_now)
        )

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
        },
    )

    logger.info("signal_engine.scoring_loop: notification sent", {
        "user_id": user_id,
        "notification_id": notification_id,
        "content_id": best_cand.content_id,
        "category": best_cand.category,
        "sub_category": best_cand.sub_category,
        "best_score": round(best_score, 3),
        "components": {k: round(v, 3) for k, v in components.items()},
    })


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
    scored: list[tuple[float, ScoredCandidate, dict[str, float]]],
    state: feature_store.SignalStoreState,
) -> tuple[float, ScoredCandidate, dict[str, float]] | None:
    """Return the best candidate from the category with the lowest affinity.

    Adds a flat +0.15 exploration bonus to its score. Returns None if all
    categories have the same affinity or there are no scored candidates.
    """
    if not scored:
        return None
    all_categories = {cand.category for _, cand, _ in scored if cand.category}
    if not all_categories:
        return None

    def category_affinity(cat: str) -> float:
        return state.category_affinities.get(cat, 0.0)

    target_category = min(all_categories, key=category_affinity)

    for score, cand, comps in scored:
        if cand.category == target_category:
            boosted_score = min(2.0, score + 0.15)
            return (boosted_score, cand, {**comps, "exploration_bonus": 0.15})
    return None


async def _load_user_timezone(user_id: str) -> str:
    def _fetch() -> str:
        doc = admin_firestore().collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone", "UTC")
        return "UTC"
    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return "UTC"


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


async def _load_recent_outcome_categories(user_id: str) -> list[str]:
    """Categories from the last N outcomes, most-recent first. Parallel fetches."""
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
        return [str((s.to_dict() or {}).get("content_id", "")) for s in snaps]

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


async def _build_framing_context(user_id: str, user_local_now: datetime) -> UserFramingContext:
    """Read top interests + dominant tone from UserAura."""
    def _fetch() -> dict[str, Any]:
        snap = admin_firestore().collection("UserAura").document(user_id).get()
        if not snap.exists:
            return {}
        return snap.to_dict() or {}

    try:
        aura = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("signal_engine.scoring_loop: UserAura read failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        aura = {}

    # Specific subjects (e.g. "KCR", "XUV 3XO") give the framer a concrete hook to
    # personalise copy; falls back to legacy free-text interests for old profiles.
    top_interests = top_interest_subjects(aura, k=3)

    return UserFramingContext(
        top_interests=top_interests,
        dominant_tone=aura.get("dominant_tone"),
        user_local_time_band=derive_local_time_band(user_local_now),
        depth_level=int(aura.get("emotional_engagement_level", 1) or 1),
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
