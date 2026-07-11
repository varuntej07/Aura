"""
Scoring pass — runs ONCE per completed 4-hour content-ingest generation, as a
durable Cloud Task enqueued by the ingest handler (2026-07-09; replaced the
recurring 15-30 min Cloud Scheduler cadence, which re-ran a near-identical
50-doc KNN per user against a pool that only changes every 4 hours — 16
scoring passes per ingest interval down to 1, a 93.75% cut in scheduled
scoring executions; see firestore_read_audit_20260706 memory for the earlier
15→30 min step). Idempotency across duplicate Cloud Task deliveries, ingest
retries, and manual recovery calls lives in generation_store.py; this module
only knows how to score one pass.

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
import re
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
from ..user_aura_schema import (
    GENDER_FIELD,
    LANGUAGE_FIELD,
    LOCALE_FIELD,
    ONBOARDING_INTERESTS_FIELD,
    active_category_slugs,
    category_label,
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
from ..notification_ledger import NotificationDecision
from ..notification_service import NotificationResult
from ..notifications import orchestrator
from ..notifications.proposal import SOURCE_NEWS, NotificationProposal, ProposalKind
from . import event_ingester, feature_store
from .content_category_map import POOL_PRODUCIBLE_TAXONOMY_SLUGS, to_taxonomy_slug
from .content_pool import (
    ScoredCandidate,
    find_nearest_for_user,
    has_any_candidate,
    list_recent_breaking_candidates,
)
from .notification_framer import (
    FRAMER_PROMPT_VERSION,
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

# Cap on the denormalized recent-sends ring buffer (feature_store.SignalStoreState
# .recent_sends). 60 matches the old _load_recent_sent_content_ids window: news
# candidates expire from the pool within ~24h, so 60 rows comfortably covers any
# item still selectable for already-sent suppression.
RECENT_SENDS_MAX = 60

# Consecutive ticks with no engagement before exploration drift activates. This
# counts TICKS, so it must scale with cadence to keep roughly the same wall-clock
# idle time (the same reasoning that halved it 20->10 when ticks widened
# 15min->30min on 2026-07-06). With ingest-triggered scoring there are only 6
# ticks per day (one per 4h generation): 2 ticks ≈ 8h idle, the closest match to
# the original ~5h intent that still requires more than a single missed pass.
EXPLORATION_DRIFT_THRESHOLD = 2

# Gate B fall-through: how many top-ranked eligible candidates to frame (one LLM
# call each) in search of one that passes the relevance gate, before giving up for
# this tick. The math ranker and the LLM relevance judge often disagree — the math
# #1 is frequently a broad-category match the framer rejects while the #2 is the
# exact match it would approve — so abandoning the tick on the first rejection
# silently drops good sends. Bounds the per-tick framer cost; still at most one send.
MAX_FRAME_ATTEMPTS = 3

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

# ── Active-tracker topic suppression (news lane vs tracking lane overlap) ─────
# "Track the FIFA World Cup" both provisions a tracker AND enriches UserAura
# interests, which steers this loop's user_vector + allow-list toward FIFA — so the
# user would get the same story twice, once from each lane. While a tracker on
# topic X delivered within this window, the news lane skips X-matching candidates;
# the tracker's fact-gated moments own that story (founder decision 2026-07-10).
TRACKER_NEWS_SUPPRESSION_WINDOW = timedelta(hours=24)

# Tokens too generic to identify a tracked topic on their own.
_TRACKER_TOPIC_TOKEN_STOPWORDS = frozenset({
    "the", "a", "an", "of", "and", "at", "in", "on", "for", "vs", "v",
    "news", "update", "updates", "latest",
})


def _topic_match_tokens(text: str) -> set[str]:
    """Lowercased alphabetic tokens for tracked-topic matching. Pure digits are
    dropped ("2026" appears in half of all sports headlines)."""
    words = re.split(r"[^a-z0-9]+", (text or "").lower())
    return {
        w for w in words
        if w and not w.isdigit() and w not in _TRACKER_TOPIC_TOKEN_STOPWORDS
    }


async def _recently_updated_tracker_token_sets(
    user_id: str, now: datetime,
) -> list[tuple[str, set[str]]]:
    """Token sets of the user's ACTIVE tracked topics that delivered an update within
    the suppression window. One auto-indexed per-user query, only on scoring passes
    (6/day per user with ingest-triggered scoring). Fails open to [] — a read error
    must never mute the news lane."""
    from ..tracking import fields as tracking_fields
    from ..tracking import tracking_store

    token_sets: list[tuple[str, set[str]]] = []
    for tracker in await tracking_store.list_trackers_for_user(user_id):
        if tracker.status != tracking_fields.TRACKER_STATUS_ACTIVE:
            continue
        if (
            tracker.last_update_at is None
            or now - tracker.last_update_at > TRACKER_NEWS_SUPPRESSION_WINDOW
        ):
            continue
        tokens = _topic_match_tokens(tracker.topic_key.replace("-", " "))
        if tokens:
            token_sets.append((tracker.topic_key, tokens))
    return token_sets


def _matched_tracked_topic(
    candidate: ScoredCandidate, token_sets: list[tuple[str, set[str]]],
) -> str:
    """The tracked topic_key this candidate covers, or "". A single-token topic
    ("tesla") matches on that token; a multi-token one ("fifa world cup") needs at
    least two shared tokens so one incidental word can't suppress unrelated news."""
    candidate_tokens = _topic_match_tokens(f"{candidate.title} {candidate.sub_category}")
    for topic_key, topic_tokens in token_sets:
        required = min(2, len(topic_tokens))
        if len(topic_tokens & candidate_tokens) >= required:
            return topic_key
    return ""


@dataclass
class TickSummary:
    users_considered: int = 0
    users_skipped_no_state: int = 0
    # Users who HAD a vector but find_nearest returned no candidates — the pool is
    # starved or vector search is failing. A first-class counter so this stops being
    # an inferred guess: it used to be a silent early-return invisible in the metrics,
    # which sent the 2026-06-14 diagnosis chasing the (healthy) vector index.
    users_skipped_no_candidates: int = 0
    # Users whose personal lane actually ran a KNN query this pass (breaking-lane
    # sends and pre-KNN skips are excluded). Persisted onto the generation record
    # so cost per pass is observable, not inferred.
    knn_queries: int = 0
    # Users whose KNN candidates were actually scored (KNN returned a non-empty set).
    users_scored: int = 0
    notifications_sent: int = 0
    blocked_below_threshold: int = 0
    blocked_daily_cap: int = 0
    blocked_quiet_hours: int = 0
    # Candidates skipped because an ACTIVE topic tracker already pushed on the same
    # subject within the last 24h (the tracker owns that story; the news lane must
    # not double-cover it).
    blocked_tracker_overlap: int = 0
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

    # Breaking-lane candidates are vector-independent — the same freshest-40 list
    # applies to every user this tick (content_pool.py docstring: "deliberately
    # VECTOR-INDEPENDENT"). Fetch once per tick and share it across all users
    # instead of re-querying it once per user (was N reads/tick, now 1).
    breaking_candidates = await list_recent_breaking_candidates(
        min_salience=BREAKING_SALIENCE_BAR, limit=40,
    )

    models = get_model_provider()
    semaphore = asyncio.Semaphore(TICK_USER_CONCURRENCY)

    async def _score_with_semaphore(user_id: str) -> None:
        async with semaphore:
            try:
                await _score_one_user(user_id, models, summary, breaking_candidates)
            except Exception as exc:
                logger.exception("signal_engine.scoring_loop: per-user failure while scoring concurrently using semaphore", {
                    "user_id": user_id,
                    "error": str(exc),
                })

    await asyncio.gather(*[_score_with_semaphore(uid) for uid in user_ids])

    logger.info("signal_engine.scoring_loop: tick complete", {
        "users_considered": summary.users_considered,
        "users_skipped_no_state": summary.users_skipped_no_state,
        "users_skipped_no_candidates": summary.users_skipped_no_candidates,
        "users_scored": summary.users_scored,
        "knn_queries": summary.knn_queries,
        "notifications_sent": summary.notifications_sent,
        "blocked_below_threshold": summary.blocked_below_threshold,
        "blocked_daily_cap": summary.blocked_daily_cap,
        "blocked_quiet_hours": summary.blocked_quiet_hours,
        "blocked_tracker_overlap": summary.blocked_tracker_overlap,
        "timeouts_swept": summary.timeouts_swept,
    })

    # One self-explanatory health line so the collapsed log view tells the whole
    # story of a tick without expanding any jsonPayload. The median below-threshold
    # score says HOW close the pool came: ~0.4 means matches are strong and the
    # threshold may be the lever; very low means the content/vector match is weak.
    below_scores = summary.blocked_below_threshold_scores
    median_below = round(statistics.median(below_scores), 3) if below_scores else None
    logger.info(
        f"signal_engine.scoring_loop: tick health: "
        f"sent={summary.notifications_sent}/{summary.users_considered} considered | "
        f"blocked: below_threshold={summary.blocked_below_threshold}"
        f"(median_score={median_below}, threshold={NOTIFICATION_SCORE_THRESHOLD}), "
        f"daily_cap={summary.blocked_daily_cap}, "
        f"quiet_hours={summary.blocked_quiet_hours}, "
        f"no_state={summary.users_skipped_no_state}, "
        f"no_candidates={summary.users_skipped_no_candidates} | "
        f"timeouts_swept={summary.timeouts_swept}",
        {
            "notifications_sent": summary.notifications_sent,
            "users_considered": summary.users_considered,
            "median_below_threshold_score": median_below,
            "notification_score_threshold": NOTIFICATION_SCORE_THRESHOLD,
        },
    )

    # Fail loud on 0 sends, but name the ACTUAL cause. has_any_candidate() now counts
    # only NON-expired docs, so the three branches below are mutually exclusive and
    # each points at one root cause instead of the old guess that blamed the vector
    # index for what was really a starved pool (2026-06-14).
    if summary.notifications_sent == 0 and summary.users_considered > 0:
        if not await has_any_candidate():
            # No FRESH candidates at all (every doc expired, or none were ingested).
            logger.warn(
                "signal_engine.scoring_loop: 0 notifications and the pool has NO FRESH "
                "candidates (all expired or none ingested) — the content-ingest job is not "
                "refreshing the pool. See the content_ingest alarm (newsdata key/quota, the "
                "Brave fallback, Gemini embedding billing). Notifications cannot send.",
                {"users_considered": summary.users_considered},
            )
        elif summary.users_skipped_no_candidates > 0:
            # Pool HAS fresh content, yet find_nearest returned [] for real users —
            # genuine vector search failure (now worth checking the embedding index)
            # or those users' vectors retrieve no live candidates.
            logger.warn(
                "signal_engine.scoring_loop: 0 notifications, pool HAS fresh content, but "
                f"vector search returned nothing for {summary.users_skipped_no_candidates} "
                "user(s) — check the find_nearest_for_user error log and the "
                "content_candidates.embedding vector index.",
                {
                    "users_considered": summary.users_considered,
                    "users_skipped_no_candidates": summary.users_skipped_no_candidates,
                },
            )
        elif (
            summary.blocked_below_threshold == 0
            and summary.blocked_daily_cap == 0
            and summary.blocked_quiet_hours == 0
        ):
            # Nobody scored and nobody was skipped for missing candidates — almost
            # always means no user has a bootstrapped interest vector yet.
            logger.warn(
                "signal_engine.scoring_loop: 0 notifications and no user reached the scoring "
                "gate — most likely no user has a bootstrapped interest vector yet (new "
                "beta accounts / Aura consent not granted).",
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
    breaking_candidates: list[ScoredCandidate],
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

    # Content already sent to this user + recent send categories for the diversity
    # tie-breaker, both derived in memory from the denormalized recent_sends ring
    # buffer on `state` (was: two separate range queries every tick). A pre-
    # existing user's buffer self-heals via a one-time backfill from the outcomes
    # subcollection. Both lanes drop already-sent content so the same story is
    # never re-sent — when the top pick equals a previous send, the next-best
    # fresh one is chosen instead.
    await _ensure_recent_sends_backfilled(user_id, state)
    sent_content_ids, recent_categories = _derive_recent_sends(state)

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
            user_local_now, user_local_date, summary, breaking_candidates,
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
    summary.knn_queries += 1
    if not candidates:
        # The user has a vector but vector search returned nothing — pool starved
        # (all candidates expired) or find_nearest is failing. Count it so the tick
        # health line and the 0-send warning name the real cause instead of guessing.
        await _safe_write_state(user_id, state)
        summary.users_skipped_no_candidates += 1
        return
    summary.users_scored += 1

    # recent_categories was already derived above, alongside sent_content_ids.

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
        chosen = max(eligible, key=lambda item: item[0] * item[1])
        have_pick = True
    else:
        # Either nothing cleared the threshold, or nothing in-interest did. Keep the
        # strongest overall match for logging; do NOT send it (Gate A would be
        # violated). have_pick=False routes to the below-threshold block path.
        chosen = max(scored, key=lambda item: item[0])
        have_pick = False
    best_score, _, best_cand, components = chosen

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
            chosen = exploration
            best_score, _, best_cand, components = chosen
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

    # The unified proactive budget + spacing is now claimed in the DRAIN (claiming here
    # too would double-count). The engine's own is_sendable DAILY_HARD_CAP above remains
    # the per-source sub-cap.
    #
    # Build the framing context once from the UserAura already read for the allow-set,
    # plus gender/language from the user doc (tone + output language). No new read.
    # Shared across all framing attempts below.
    user_context = _build_framing_context(aura, user_doc, user_local_now)

    # Gate B fall-through (Fix): frame the top few eligible candidates in rank order
    # and send the FIRST that passes the relevance gate, rather than abandoning the
    # whole tick when only the #1 is rejected. The chosen pick (incl. exploration
    # drift) is tried first. Capped at MAX_FRAME_ATTEMPTS LLM calls; still one send.
    attempts = _ordered_frame_attempts(eligible, chosen, MAX_FRAME_ATTEMPTS)

    # Active-tracker suppression: a topic the user's tracker already pushed on within
    # the window belongs to the tracking lane — the news lane skips its candidates so
    # the same story never arrives twice from two lanes.
    tracked_token_sets = await _recently_updated_tracker_token_sets(user_id, datetime.now(UTC))
    if tracked_token_sets:
        remaining = []
        for attempt in attempts:
            overlapping_topic = _matched_tracked_topic(attempt[2], tracked_token_sets)
            if overlapping_topic:
                summary.blocked_tracker_overlap += 1
                logger.info(
                    "signal_engine.scoring_loop: candidate suppressed, active tracker "
                    f"already covers it (topic={overlapping_topic})",
                    {
                        "user_id": user_id,
                        "content_id": attempt[2].content_id,
                        "topic_key": overlapping_topic,
                    },
                )
                continue
            remaining.append(attempt)
        attempts = remaining
        if not attempts:
            state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
            await _safe_write_state(user_id, state)
            return
    framed = None
    relevance_reason = ""
    for attempt_score, _, attempt_cand, attempt_components in attempts:
        try:
            candidate_framed = await asyncio.wait_for(
                frame_notification(models, attempt_cand, user_context), timeout=10.0
            )
        except TimeoutError:
            logger.warn("signal_engine.scoring_loop: framer LLM timed out, using fallback", {
                "user_id": user_id,
                "content_id": attempt_cand.content_id,
            })
            candidate_framed = _safe_fallback(attempt_cand)

        # Framer infra outage (not a content rejection): the framer is down for this
        # tick, so trying more candidates is pointless. Defer the whole tick and
        # scream so a sustained outage never looks like "nothing was relevant".
        if candidate_framed.relevance_reason == FRAMER_UNAVAILABLE_REASON:
            state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
            await _safe_write_state(user_id, state)
            summary.blocked_below_threshold += 1
            logger.warn(
                "signal_engine.scoring_loop: not sending, framer UNAVAILABLE "
                "(deferring this tick, infra not relevance; retries next tick)",
                {
                    "user_id": user_id,
                    "content_id": attempt_cand.content_id,
                    "category": attempt_cand.category,
                },
            )
            return

        # Gate B — relevance contract, fail-CLOSED on a missing reason. A send fires
        # only when the framer affirmed relevance AND named the interest it matches.
        attempt_reason = (candidate_framed.relevance_reason or "").strip()
        if candidate_framed.is_relevant and attempt_reason:
            framed = candidate_framed
            best_score, best_cand, components = attempt_score, attempt_cand, attempt_components
            relevance_reason = attempt_reason
            break
        logger.info(
            "signal_engine.scoring_loop: candidate failed relevance gate, trying next "
            f"({'no reason given' if candidate_framed.is_relevant else 'not relevant'})",
            {
                "user_id": user_id,
                "content_id": attempt_cand.content_id,
                "category": attempt_cand.category,
            },
        )

    if framed is None:
        # Every attempted candidate failed the relevance gate this tick. Recover next
        # tick as the pool refreshes — never a permanent mute.
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
        await _safe_write_state(user_id, state)
        summary.blocked_below_threshold += 1
        logger.info(
            f"signal_engine.scoring_loop: not sending (relevance gate: all "
            f"{len(attempts)} attempted candidate(s) rejected)",
            {"user_id": user_id, "content_id": best_cand.content_id},
        )
        return

    # Hand ONE proposal to the funnel. Cross-agent dedup, priority arbitration (vs
    # thread/icebreaker/re-engage), the tap-worthiness gate, the unified adaptive budget,
    # and smart-timing all run in the DRAIN. The delivery-dependent bookkeeping (the
    # learning outcome, the funnel event, sends_today++) runs in on_news_delivered when
    # the drain actually delivers — so it can never count a held/dropped proposal.
    notification_id = str(uuid.uuid4())
    await orchestrator.submit(
        NotificationProposal(
            user_id=user_id,
            source=SOURCE_NEWS,
            kind=ProposalKind.PROACTIVE,
            dedup_key=best_cand.content_id,
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
                # Buddy-facing "why I reached out" — injected into the chat prompt on the
                # FIRST turn after a tap so Buddy stays oriented instead of disowning its
                # own opener. Never shown in the push itself.
                "notification_reason": relevance_reason,
                "notification_origin": "signal_engine",
            },
            notification_type="signal_engine",
            collapse_key=f"signal_{notification_id}",
            # Real freshness: the candidate's own timestamp drives the 18h news window.
            content_timestamp=best_cand.freshness_ts,
            decision=NotificationDecision(
                score=best_score,
                components=components,
                gate_a_active=gate_a_active,
                matched_interest_slug=best_cand.category,
                relevance_reason=relevance_reason,
                framer_prompt_version=FRAMER_PROMPT_VERSION,
                sends_today_before=state.sends_today,
                local_hour=user_local_now.hour,
                day_of_week=user_local_now.weekday(),
            ),
        )
    )
    # Persist this tick's IN-TICK state mutations (daily reset, vector bootstrap/refresh,
    # timeout-driven no-open bumps). NOT sends_today — that is a delivery fact the hook
    # owns. Safe from clobber: no other tick runs for this user between enqueue and the
    # within-the-minute drain that re-reads this state.
    await _safe_write_state(user_id, state)
    # Counts "handed to the funnel" — the metric the tick-health line + 0-send warning
    # care about (did scoring produce something to send?). The real delivery is logged
    # by the drain and recorded in on_news_delivered.
    summary.notifications_sent += 1

    logger.info(
        f"signal_engine.scoring_loop: notification enqueued "
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


async def on_news_delivered(
    proposal: NotificationProposal, result: NotificationResult
) -> None:
    """Post-send bookkeeping for a signal-engine (news) push the drain DELIVERED.

    Runs in the drain (via post_send.dispatch_post_send), NOT the scoring tick, so the
    learning outcome, the funnel event, and the daily send counters all key off a REAL
    delivery — a held or dropped proposal records nothing. Re-reads state fresh: the
    producer already persisted this tick's vector / daily-reset mutations at enqueue, and
    no other tick runs for this user between enqueue and the within-the-minute drain, so
    this read-modify-write of the counters can't clobber them. Never raises.
    """
    user_id = proposal.user_id
    data = proposal.data or {}
    decision = proposal.decision
    is_breaking = data.get("lane") == "breaking"
    state = await feature_store.read_state(user_id)

    if not result.delivered:
        state.consecutive_no_open_ticks = min(100, state.consecutive_no_open_ticks + 1)
        await _safe_write_state(user_id, state)
        logger.info("signal_engine.scoring_loop: news send returned no delivery", {
            "user_id": user_id, "notification_id": data.get("notification_id", ""),
        })
        return

    now = datetime.now(UTC)
    state.sends_today += 1
    if is_breaking:
        state.breaking_sends_today += 1
    state.last_notification_at = now
    content_id = str(data.get("content_id", ""))
    category = str(data.get("category", ""))
    # Append to the denormalized ring buffer the scoring tick reads from (was: a
    # separate range query). Safe even if this user's buffer hasn't been
    # backfilled yet — the next tick's backfill re-reads the outcomes
    # subcollection this send is about to land in, so nothing here is lost.
    feature_store.record_recent_send(
        state, content_id=content_id, category=category, sent_at=now, cap=RECENT_SENDS_MAX,
    )
    await _safe_write_state(user_id, state)

    notification_id = data.get("notification_id", "")
    score = decision.score if (decision and decision.score is not None) else 0.0
    relevance_reason = decision.relevance_reason if decision else ""
    await feature_store.write_outcome_pending(
        user_id,
        notification_id,
        content_id=content_id,
        score=score,
        scored_at=now,
        sent_at=now,
        relevance_reason=relevance_reason,
        category=category,
    )

    # Top of the re-engagement funnel — the property keys must match the client's tap
    # event so PostHog can join sent -> tapped -> session -> action.
    properties: dict[str, Any] = {
        PROP_NOTIFICATION_ID: notification_id,
        PROP_CONTENT_ID: data.get("content_id", ""),
        PROP_CATEGORY: data.get("category", ""),
        PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_SIGNAL_ENGINE,
        "sub_category": data.get("sub_category", ""),
        "source": data.get("source", ""),
        "relevance_reason": relevance_reason,
        "score": round(score, 4),
    }
    if is_breaking:
        properties["lane"] = "breaking"
    await posthog_client.capture_event(
        distinct_id=user_id, event=EVENT_NOTIFICATION_SENT, properties=properties,
    )

    logger.info("signal_engine.scoring_loop: news notification delivered", {
        "user_id": user_id, "notification_id": notification_id,
        "content_id": data.get("content_id", ""), "lane": data.get("lane", "personal"),
    })


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


def _ordered_frame_attempts(
    eligible: list[tuple[float, float, ScoredCandidate, dict[str, float]]],
    chosen: tuple[float, float, ScoredCandidate, dict[str, float]],
    cap: int,
) -> list[tuple[float, float, ScoredCandidate, dict[str, float]]]:
    """Frame-attempt order for the Gate B fall-through: the chosen pick first (it may
    be an exploration-drift candidate that is not the top of `eligible`), then the
    remaining threshold-clearing, in-allow-set candidates by base * diversity, deduped
    by content_id and capped at `cap`. Sending stops at the first that passes Gate B."""
    chosen_id = chosen[2].content_id
    rest = sorted(
        (item for item in eligible if item[2].content_id != chosen_id),
        key=lambda item: item[0] * item[1],
        reverse=True,
    )
    return ([chosen] + rest)[:cap]


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


def _derive_recent_sends(
    state: feature_store.SignalStoreState, *, now: datetime | None = None,
) -> tuple[set[str], list[str]]:
    """Pure, in-memory replacement for the two per-tick range queries this used to
    be: already-sent content_ids (any outcome, whole ring buffer) and the most
    recent categories within DIVERSITY_LOOKBACK_HOURS (capped at
    RECENT_OUTCOMES_FOR_DIVERSITY), most-recent first. state.recent_sends is
    stored oldest-first (feature_store.record_recent_send appends), so walking it
    in reverse gives newest-first; the walk stops at the first entry outside the
    lookback window since everything after it is even older."""
    now = now or datetime.now(UTC)
    cutoff = now - timedelta(hours=DIVERSITY_LOOKBACK_HOURS)

    sent_content_ids = {e["content_id"] for e in state.recent_sends if e.get("content_id")}

    recent_categories: list[str] = []
    for entry in reversed(state.recent_sends):
        sent_at = entry.get("sent_at")
        if isinstance(sent_at, datetime):
            aware = sent_at if sent_at.tzinfo else sent_at.replace(tzinfo=UTC)
            if aware < cutoff:
                break
        category = entry.get("category")
        if category:
            recent_categories.append(category)
        if len(recent_categories) >= RECENT_OUTCOMES_FOR_DIVERSITY:
            break
    return sent_content_ids, recent_categories


async def _load_recent_sent_rows(user_id: str, limit: int = RECENT_SENDS_MAX) -> list[dict[str, Any]]:
    """ONE-TIME backfill query (see _ensure_recent_sends_backfilled) — not called
    per tick. Returns rows oldest-first (matching how record_recent_send appends
    new entries), each {"content_id", "category", "sent_at"}. Older outcome docs
    predate the ``category`` field on write_outcome_pending, so category may come
    back empty here; the caller joins content_pool for just the handful that
    still need it. Returns [] on error (fail-open — a failed backfill just means
    dedup/diversity stay cold for this tick, never a mute)."""
    def _fetch() -> list[dict[str, Any]]:
        db = admin_firestore()
        snaps = (
            db.collection("users").document(user_id)
            .collection("signal_store").document("state")
            .collection("outcomes")
            .order_by("sent_at", direction="DESCENDING")
            .limit(limit)
            .stream()
        )
        rows: list[dict[str, Any]] = []
        for s in snaps:
            data = s.to_dict() or {}
            content_id = str(data.get("content_id", "") or "")
            if not content_id:
                continue
            sent_at = data.get("sent_at")
            rows.append({
                "content_id": content_id,
                "category": str(data.get("category", "") or ""),
                "sent_at": sent_at if isinstance(sent_at, datetime) else datetime.now(UTC),
            })
        rows.reverse()  # oldest-first
        return rows

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("signal_engine.scoring_loop: recent-sends backfill fetch failed", {
            "user_id": user_id, "error": str(exc),
        })
        return []


async def _ensure_recent_sends_backfilled(
    user_id: str, state: feature_store.SignalStoreState,
) -> None:
    """One-time migration for pre-existing users: recent_sends didn't exist before
    this denormalization, so the first tick after deploy backfills it from the
    outcomes subcollection instead of leaving already-sent suppression / the
    diversity tie-breaker silently cold until the ring buffer organically refills
    over the next RECENT_SENDS_MAX real sends. Mutates state in place; the
    caller's existing state write persists it. No-op once recent_sends_backfilled
    is set — including for a genuinely-empty history — so this never re-queries
    on a later tick."""
    if state.recent_sends_backfilled:
        return

    rows = await _load_recent_sent_rows(user_id, limit=RECENT_SENDS_MAX)

    # Only the most recent RECENT_OUTCOMES_FOR_DIVERSITY rows ever fed the
    # diversity tie-breaker under the old two-query design; the rest only ever
    # fed the content_id dedup set, which needs no category. Join content_pool
    # ONLY for legacy rows missing the (now-inline) category field, and only for
    # that handful — this is a one-time cost, not a per-tick one.
    if rows:
        from .content_pool import get_candidate

        needs_category = [r for r in rows[-RECENT_OUTCOMES_FOR_DIVERSITY:] if not r["category"]]
        if needs_category:
            candidates = await asyncio.gather(
                *[get_candidate(r["content_id"]) for r in needs_category]
            )
            for row, cand in zip(needs_category, candidates):
                if cand and cand.category:
                    row["category"] = cand.category

    state.recent_sends = rows
    state.recent_sends_backfilled = True
    logger.info("signal_engine.scoring_loop: backfilled recent_sends ring buffer", {
        "user_id": user_id, "count": len(rows),
    })


async def _try_send_breaking(
    user_id: str,
    models: ModelProvider,
    state: feature_store.SignalStoreState,
    user_doc: dict[str, Any],
    sent_content_ids: set[str],
    user_local_now: datetime,
    user_local_date: str,
    summary: TickSummary,
    breaking_candidates: list[ScoredCandidate],
) -> bool:
    """Lane B: try to send ONE globally-significant breaking story, bypassing the
    personal interest gate. Returns True iff a notification was delivered (the
    caller then persists state). Vector-independent — reads the freshest high-
    salience pool items directly, not the user's vector neighbours.

    ``breaking_candidates`` is fetched ONCE per tick by the caller (run_tick) and
    shared across every user, since the query itself has no user-specific filter.

    Mutates state (sends_today, breaking_sends_today, last_notification_at) on a
    successful send; the caller writes state. Every other branch leaves state for
    the personal lane to continue."""
    pick = next((c for c in breaking_candidates if c.content_id not in sent_content_ids), None)
    if pick is None:
        return False

    # The unified proactive budget is claimed in the DRAIN now (not here).
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

    # Enqueue ONE breaking proposal. The drain owns dedup/arbitration/tap-gate/budget;
    # on_news_delivered (lane="breaking") bumps the delivery counters on a real send. The
    # caller (_score_one_user) still persists this tick's in-tick state mutations.
    notification_id = str(uuid.uuid4())
    await orchestrator.submit(
        NotificationProposal(
            user_id=user_id,
            source=SOURCE_NEWS,
            kind=ProposalKind.PROACTIVE,
            dedup_key=pick.content_id,
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
                # Buddy-facing "why I reached out", injected into the chat prompt on the
                # first turn after a tap (never shown in the push).
                "notification_reason": framed.relevance_reason or "globally significant breaking news",
                "notification_origin": "signal_engine",
                # Breaking reuses the signal_engine origin so the existing client tap
                # routing + funnel join work unchanged; `lane` only separates analytics
                # and tells the drain (smart-timing) + on_news_delivered this is breaking.
                "lane": "breaking",
            },
            notification_type="signal_engine",
            collapse_key=f"signal_{notification_id}",
            content_timestamp=pick.freshness_ts,
            decision=NotificationDecision(
                score=pick.salience,
                matched_interest_slug=pick.category,
                relevance_reason=framed.relevance_reason or "globally significant breaking news",
                framer_prompt_version=FRAMER_PROMPT_VERSION,
                lane="breaking",
                sends_today_before=state.sends_today,
                local_hour=user_local_now.hour,
                day_of_week=user_local_now.weekday(),
            ),
        )
    )
    summary.notifications_sent += 1

    logger.info(
        f"signal_engine.scoring_loop: BREAKING notification enqueued "
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
    # personalise copy. A brand-new user has none yet (onboarding seeds categories
    # with no subjects), which used to leave the framer with "none recorded yet" and
    # made its name-a-subject relevance gate reject everything — cold-start starvation.
    # Fall back to the declared category LABELS so the framer's cold-start branch can
    # match at category level until the extractor learns subjects from chat.
    subjects = top_interest_subjects(aura, k=3)
    has_specific = bool(subjects)
    if has_specific:
        top_interests = subjects
    else:
        slugs = active_category_slugs(aura)
        if not slugs:
            declared = user_doc.get(ONBOARDING_INTERESTS_FIELD)
            slugs = [str(s) for s in declared if s] if isinstance(declared, list) else []
        top_interests = [category_label(s) for s in slugs[:3]]

    language = str(user_doc.get(LANGUAGE_FIELD, "") or "").strip() or "English"
    gender = str(user_doc.get(GENDER_FIELD, "") or "").strip() or None
    # display_name is the field the writer (UserModel.toJson) stores and the voice
    # fetcher (voice/fetchers.py) already reads. Drop the literal "User" creation
    # fallback so the framer never addresses someone as "User".
    raw_name = str(user_doc.get("display_name", "") or "").strip()
    name = raw_name if raw_name and raw_name != "User" else None

    return UserFramingContext(
        top_interests=top_interests,
        dominant_tone=aura.get("dominant_tone"),
        user_local_time_band=derive_local_time_band(user_local_now),
        depth_level=int(aura.get("emotional_engagement_level", 1) or 1),
        gender=gender,
        language=language,
        has_specific_interests=has_specific,
        name=name,
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
