"""
Session-open recommender. Uses the same scoring math as scoring_loop but
without fatigue, without daily cap, and with a stronger diversity penalty.

Used by the Daily Briefing (``rank_session``) to pick the top-ranked pool items
to weave into a user's morning digest.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime

from ...lib.logger import logger
from ..firebase import admin_firestore
from .content_pool import ScoredCandidate, find_nearest_for_user
from .feature_store import read_state
from .scoring import (
    SAME_NOTIFICATION_CATEGORY_DIVERSITY_PENALTY,
    cosine_similarity,
    freshness_decay,
)

DEFAULT_FEED_LIMIT = 20
MAX_FEED_LIMIT = 50

# Stronger same-category penalty so the in-app feed never feels repetitive.
SESSION_DIVERSITY_PENALTY = SAME_NOTIFICATION_CATEGORY_DIVERSITY_PENALTY * 0.8


@dataclass
class FeedItem:
    content_id: str
    source: str
    category: str
    title: str
    body: str
    url: str
    score: float


async def rank_session(user_id: str, limit: int = DEFAULT_FEED_LIMIT) -> list[FeedItem]:
    """Return the top-N feed items for this user, diversity-penalised."""
    limit = max(1, min(MAX_FEED_LIMIT, limit))

    state = await read_state(user_id)
    if not state.user_vector or not any(abs(x) > 1e-9 for x in state.user_vector):
        return []

    candidates = await find_nearest_for_user(state.user_vector, limit=limit * 3)
    if not candidates:
        return []

    user_local_now = await _load_user_local_now(user_id)
    return _rank_with_diversity(candidates, state.user_vector, user_local_now, limit)


def _rank_with_diversity(
    candidates: list[ScoredCandidate],
    user_vector: list[float],
    user_local_now: datetime,
    limit: int,
) -> list[FeedItem]:
    scored: list[tuple[float, ScoredCandidate]] = []
    for cand in candidates:
        cosine = cand.cosine_similarity or cosine_similarity(user_vector, cand.embedding)
        fresh = freshness_decay(cand.freshness_ts, now=user_local_now)
        scored.append((cosine * fresh, cand))
    scored.sort(key=lambda kv: kv[0], reverse=True)

    selected: list[FeedItem] = []
    seen_categories: list[str] = []
    for base_score, cand in scored:
        if len(selected) >= limit:
            break
        penalty = SESSION_DIVERSITY_PENALTY if cand.category and cand.category in seen_categories else 1.0
        final = base_score * penalty
        selected.append(FeedItem(
            content_id=cand.content_id,
            source=cand.source,
            category=cand.category,
            title=cand.title,
            body=cand.body,
            url=cand.url,
            score=final,
        ))
        if cand.category:
            seen_categories.append(cand.category)

    selected.sort(key=lambda item: item.score, reverse=True)
    return selected


async def _load_user_local_now(user_id: str) -> datetime:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    def _fetch_tz() -> str:
        doc = admin_firestore().collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone", "UTC")
        return "UTC"

    try:
        tz_name = await asyncio.to_thread(_fetch_tz)
    except Exception as exc:
        logger.warn("recommender: timezone fetch failed", {"user_id": user_id, "error": str(exc)})
        tz_name = "UTC"

    try:
        return datetime.now(ZoneInfo(tz_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)
