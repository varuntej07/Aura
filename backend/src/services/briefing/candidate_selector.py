"""Picks the buzzing-across-categories items the BriefingAgent writes up.

The briefing collects ~7-10 genuinely buzzing items spanning 3-4 categories so the
screen reads like a quick news scan, and it must work for EVERY user including a
day-one account with no interest vector. So selection is buzz-first (global salience
+ freshness, vector-independent) with personalization layered on only as a boost when
a user vector exists, rather than the old purely-personalized rank_session that
returned nothing for cold-start users.

``select_briefing_items`` does the IO (pool fetch + signal-store read); the ranking and
category round-robin live in the pure ``rank_and_diversify`` so they unit-test without
Firestore.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ...config.settings import settings
from ...lib.logger import logger
from ..signal_engine.content_pool import ScoredCandidate, list_recent_candidates_full
from ..signal_engine.feature_store import read_state
from ..signal_engine.scoring import cosine_similarity, freshness_decay

# Score blend. Without a user vector the similarity term is simply absent (buzz + fresh
# decide), which is what lets a cold-start user still get a full, current briefing.
SIMILARITY_WEIGHT = 0.5
SALIENCE_WEIGHT = 0.3
FRESHNESS_WEIGHT = 0.2

# An item whose body is shorter than this carries no real substance to write up (e.g. a
# headline with only engagement counts), so it is dropped before the model ever sees it.
MIN_BODY_CHARS = 120


@dataclass
class SelectedItem:
    content_id: str
    source: str
    category: str
    title: str
    body: str
    url: str
    score: float


def _has_vector(user_vector: list[float] | None) -> bool:
    return bool(user_vector) and any(abs(x) > 1e-9 for x in user_vector)


def rank_and_diversify(
    candidates: list[ScoredCandidate],
    user_vector: list[float] | None,
    now: datetime,
    *,
    max_items: int,
    max_per_category: int,
) -> list[SelectedItem]:
    """Pure selection: score, then round-robin across categories for spread.

    Round-robin (one item from each category per pass, strongest category first) is what
    guarantees 3-4 categories show up instead of the top slots all being one hot topic,
    while ``max_per_category`` caps any single category's share.
    """
    use_vector = _has_vector(user_vector)
    scored: list[SelectedItem] = []
    for cand in candidates:
        if len((cand.body or "").strip()) < MIN_BODY_CHARS:
            continue
        fresh = freshness_decay(cand.freshness_ts, now=now)
        score = SALIENCE_WEIGHT * cand.salience + FRESHNESS_WEIGHT * fresh
        if use_vector:
            score += SIMILARITY_WEIGHT * cosine_similarity(user_vector or [], cand.embedding)
        scored.append(SelectedItem(
            content_id=cand.content_id,
            source=cand.source,
            category=cand.category,
            title=cand.title,
            body=cand.body,
            url=cand.url,
            score=score,
        ))

    buckets: dict[str, list[SelectedItem]] = {}
    for item in scored:
        buckets.setdefault(item.category or "other", []).append(item)
    for items in buckets.values():
        items.sort(key=lambda it: it.score, reverse=True)

    # Strongest category (by its top item) leads each round.
    category_order = sorted(
        buckets,
        key=lambda c: buckets[c][0].score if buckets[c] else 0.0,
        reverse=True,
    )

    selected: list[SelectedItem] = []
    taken_per_category: dict[str, int] = {c: 0 for c in category_order}
    while len(selected) < max_items:
        progressed = False
        for category in category_order:
            if len(selected) >= max_items:
                break
            if taken_per_category[category] >= max_per_category:
                continue
            idx = taken_per_category[category]
            if idx >= len(buckets[category]):
                continue
            selected.append(buckets[category][idx])
            taken_per_category[category] += 1
            progressed = True
        if not progressed:
            break

    selected.sort(key=lambda it: it.score, reverse=True)
    return selected


async def select_briefing_items(
    user_id: str,
    *,
    region: str | None,
    now: datetime | None = None,
) -> list[SelectedItem]:
    """Fetch the recent pool and return the diversified briefing set for this user."""
    current = now or datetime.now(UTC)
    candidates = await list_recent_candidates_full(
        limit=settings.BRIEFING_POOL_SCAN_LIMIT, region=region, now=current,
    )
    if not candidates:
        return []

    state = await read_state(user_id)
    selected = rank_and_diversify(
        candidates,
        state.user_vector,
        current,
        max_items=settings.BRIEFING_ITEMS_MAX,
        max_per_category=settings.BRIEFING_MAX_PER_CATEGORY,
    )
    logger.info("briefing.selector: selected items", {
        "user_id": user_id,
        "pool": len(candidates),
        "selected": len(selected),
        "categories": len({it.category for it in selected}),
    })
    return selected
