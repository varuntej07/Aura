"""
apply_event: turn one user event into a state update.

Pure logic on the math side, I/O on the persistence side. The function:

  1. Reads the current SignalStoreState (and bootstraps on first run).
  2. If the event refers to a known content_id, nudges user_vector toward
     (or away from) the content's embedding using an EMA.
  3. Updates per-category affinity, time-slot open rate, and fatigue
     counters when applicable.
  4. Writes the new state back.

Outcome-driven updates (notification_opened / dismissed / timeout) are
handled here too. They additionally call feature_store.resolve_outcome to
flip the outcome row.

This module never sends notifications, never decides what to send, never
holds per-request state on its functions or globals.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

from ...lib.logger import logger
from . import content_pool, feature_store
from .embedder import embed_text, embed_texts
from .feature_store import (
    TIME_SLOTS_PER_DAY,
    USER_VECTOR_DIMENSION,
    SignalStoreState,
)

# Base learning rate. Negative-weighted events use the same alpha but flip sign.
USER_VECTOR_EMA_ALPHA = 0.05

# Blend rate for periodic Aura refreshes. Higher than EMA_ALPHA so idle users
# drift meaningfully toward updated interests within a few days.
AURA_REFRESH_EMA_ALPHA = 0.3

# How fast the per-slot open rate moves toward each new outcome.
SLOT_EMA_ALPHA = 0.2

# How fast category affinity drifts with each event.
CATEGORY_AFFINITY_EMA_ALPHA = 0.1

# Per-event weights driving the user_vector EMA.
EVENT_WEIGHTS: dict[str, float] = {
    "notification_opened": 1.0,
    "notification_dismissed": -0.6,
    "content_view_long": 0.8,
    "content_view_short": -0.4,
    "content_liked": 1.0,
    "content_shared": 1.2,
    "content_skipped": -0.5,
    "search_query": 0.5,
    # app_open and timeout are zero-weight for the vector. 
    # They still update other features (time-slot rate, outcome resolution).
    "app_open": 0.0,
    "notification_timeout": -0.2,
}

# Threshold under which content_view duration counts as "short" (negative signal).
SHORT_VIEW_MAX_MS = 3_000

# Threshold above which content_view duration counts as "long" (positive signal).
LONG_VIEW_MIN_MS = 20_000


async def apply_event(
    user_id: str,
    *,
    event_type: str,
    content_id: str | None = None,
    category: str | None = None,
    duration_ms: int | None = None,
    search_query_text: str | None = None,
    user_local_hour: int | None = None,
    user_local_minute: int | None = None,
) -> None:
    """Apply one event end-to-end. Never raises. Errors are logged and swallowed."""
    try:
        await _apply_event_inner(
            user_id,
            event_type=_normalise_event_type(event_type, duration_ms),
            content_id=content_id,
            category=category,
            search_query_text=search_query_text,
            user_local_hour=user_local_hour,
            user_local_minute=user_local_minute,
        )
        await feature_store.append_event(
            user_id,
            event_type=event_type,
            content_id=content_id,
            category=category,
            duration_ms=duration_ms,
        )
    except Exception as exc:
        logger.warn("event_ingester.apply_event failed", {
            "user_id": user_id,
            "event_type": event_type,
            "error": str(exc),
        })


async def _apply_event_inner(
    user_id: str,
    *,
    event_type: str,
    content_id: str | None,
    category: str | None,
    search_query_text: str | None,
    user_local_hour: int | None,
    user_local_minute: int | None,
) -> None:
    state = await feature_store.read_state(user_id)
    if not state.bootstrap_done:
        state = await _bootstrap_user_vector(user_id, state)

    target_embedding: list[float] | None = None
    target_category: str | None = category

    # Outcome events flip the outcome row and use the stored content for the embedding.
    if event_type in ("notification_opened", "notification_dismissed"):
        outcome_name = "opened" if event_type == "notification_opened" else "dismissed"
        if content_id:
            prev = await feature_store.resolve_outcome(user_id, content_id, outcome=outcome_name)
            if prev and prev.get("content_id"):
                cand = await content_pool.get_candidate(str(prev["content_id"]))
                if cand:
                    target_embedding = cand.embedding
                    target_category = target_category or cand.category
        _bump_time_slot_rate(
            state,
            opened=(event_type == "notification_opened"),
            user_local_hour=user_local_hour,
            user_local_minute=user_local_minute,
        )

    elif event_type in ("content_view_long", "content_view_short", "content_liked",
                        "content_shared", "content_skipped"):
        if content_id:
            cand = await content_pool.get_candidate(content_id)
            if cand:
                target_embedding = cand.embedding
                target_category = target_category or cand.category

    elif event_type == "search_query" and search_query_text:
        target_embedding = await embed_text(search_query_text)

    elif event_type == "app_open":
        # Pure time-slot ping: prove the user opens the app at this hour.
        _bump_time_slot_rate(
            state,
            opened=True,
            user_local_hour=user_local_hour,
            user_local_minute=user_local_minute,
        )

    weight = EVENT_WEIGHTS.get(event_type, 0.0)
    if target_embedding and weight != 0.0:
        _apply_vector_ema(state, target_embedding, weight)
        if target_category:
            _apply_category_ema(state, target_category, weight)

    # Any positive engagement resets the exploration drift counter.
    if event_type in ("notification_opened", "content_liked", "content_shared"):
        state.consecutive_no_open_ticks = 0

    state.last_updated = datetime.now(UTC)
    await feature_store.write_state(user_id, state)


def _normalise_event_type(event_type: str, duration_ms: int | None) -> str:
    """A bare 'content_view' event gets split into long / short based on duration."""
    if event_type != "content_view":
        return event_type
    if duration_ms is None:
        return "content_view_short"
    if duration_ms >= LONG_VIEW_MIN_MS:
        return "content_view_long"
    if duration_ms <= SHORT_VIEW_MAX_MS:
        return "content_view_short"
    return "content_view_short"


def _apply_vector_ema(state: SignalStoreState, content_vector: list[float], weight: float) -> None:
    """user_vector = user_vector + alpha * weight * (content_vector - user_vector)."""
    if len(content_vector) != USER_VECTOR_DIMENSION:
        logger.warn("event_ingester: content vector dim mismatch", {
            "expected": USER_VECTOR_DIMENSION,
            "got": len(content_vector),
        })
        return
    alpha = USER_VECTOR_EMA_ALPHA * weight
    updated = [
        u + alpha * (c - u)
        for u, c in zip(state.user_vector, content_vector)
    ]
    state.user_vector = updated


def _apply_category_ema(state: SignalStoreState, category: str, weight: float) -> None:
    target = 1.0 if weight > 0 else 0.0
    alpha = CATEGORY_AFFINITY_EMA_ALPHA * abs(weight)
    current = state.category_affinities.get(category, 0.5)
    state.category_affinities[category] = current + alpha * (target - current)


def _bump_time_slot_rate(
    state: SignalStoreState,
    *,
    opened: bool,
    user_local_hour: int | None,
    user_local_minute: int | None,
) -> None:
    if user_local_hour is None:
        return
    minute = user_local_minute or 0
    slot = (user_local_hour * 2 + (1 if minute >= 30 else 0)) % TIME_SLOTS_PER_DAY
    outcome_score = 1.0 if opened else 0.0
    current = state.time_slot_open_rates[slot]
    state.time_slot_open_rates[slot] = (
        current * (1 - SLOT_EMA_ALPHA) + outcome_score * SLOT_EMA_ALPHA
    )


async def _bootstrap_user_vector(user_id: str, state: SignalStoreState) -> SignalStoreState:
    """Initial user_vector = mean of embeddings for top deep_interests in UserAura.

    Falls back to a zero vector when UserAura is absent (consent not granted)
    or has no usable interests. bootstrap_done is set either way.
    """
    interests = await _read_top_deep_interests(user_id, top_k=10)
    if interests:
        try:
            vectors = await embed_texts(interests)
            if vectors:
                avg = _average_vectors(vectors)
                state.user_vector = avg
                logger.info("event_ingester: bootstrapped user_vector from UserAura", {
                    "user_id": user_id,
                    "interest_count": len(interests),
                })
        except Exception as exc:
            logger.warn("event_ingester: bootstrap embedding failed, using zero vector", {
                "user_id": user_id,
                "error": str(exc),
            })
    state.bootstrap_done = True
    return state


def _average_vectors(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        return [0.0] * USER_VECTOR_DIMENSION
    dim = len(vectors[0])
    sums = [0.0] * dim
    for v in vectors:
        for i in range(dim):
            sums[i] += v[i]
    n = float(len(vectors))
    return [s / n for s in sums]


async def refresh_user_vector_from_aura(
    user_id: str,
    state: SignalStoreState,
) -> SignalStoreState:
    """Blend user_vector toward current UserAura top interests.

    Called by the scoring loop every AURA_REFRESH_INTERVAL_DAYS for users who
    haven't sent any events (so the EMA never fires). Uses a higher alpha than
    the per-event EMA so that evolved interests surface within a few days even
    for completely idle users.
    """
    from datetime import datetime
    interests = await _read_top_deep_interests(user_id, top_k=10)
    if interests:
        try:
            vectors = await embed_texts(interests)
            if vectors:
                aura_avg = _average_vectors(vectors)
                state.user_vector = [
                    (1 - AURA_REFRESH_EMA_ALPHA) * u + AURA_REFRESH_EMA_ALPHA * a
                    for u, a in zip(state.user_vector, aura_avg)
                ]
                logger.info("event_ingester: periodic Aura refresh applied", {
                    "user_id": user_id,
                    "interest_count": len(interests),
                })
        except Exception as exc:
            logger.warn("event_ingester: Aura refresh embedding failed", {
                "user_id": user_id,
                "error": str(exc),
            })
    state.last_bootstrap_at = datetime.now(UTC)
    return state


async def _read_top_deep_interests(user_id: str, top_k: int) -> list[str]:
    """Pull deep_interest_frequencies from UserAura and return the top_k keys."""
    import asyncio

    from ..firebase import admin_firestore

    def _fetch() -> dict[str, int]:
        snap = admin_firestore().collection("UserAura").document(user_id).get()
        if not snap.exists:
            return {}
        data = snap.to_dict() or {}
        freq = data.get("deep_interest_frequencies") or {}
        return {str(k): int(v) for k, v in freq.items()} if isinstance(freq, dict) else {}

    try:
        freq_map = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("event_ingester: read deep_interests failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        return []

    if not freq_map:
        return []
    ranked: Iterable[tuple[str, int]] = sorted(freq_map.items(), key=lambda kv: kv[1], reverse=True)
    return [k for k, _ in list(ranked)[:top_k]]
