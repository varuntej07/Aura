"""
The ONE category vocabulary contract for the signal engine.

The content pool, the per-user interest taxonomy (UserAura), the onboarding
picker, the scoring gate, category affinities, diversity, and the feed must all
speak the SAME category language. They historically did not: fetchers emitted a
small ad-hoc vocabulary ({tech, research, news, business, science, sports}) while
UserAura / onboarding used the closed taxonomy slugs in ``user_aura_schema``
({technology_computing, science_nature, ...}). Only ``sports`` was identical, so
a category allow-list gate (``candidate.category in allow_set``) silently filtered
out nearly everything — the exact "zero rows looks healthy" failure this project
has been bitten by (see CLAUDE.md data-layer discipline).

This module is the single source of truth that maps the fetcher/source vocabulary
onto the taxonomy slugs. Normalisation happens at ONE write choke point
(``content_pool.add_candidates``) so the pool only ever stores taxonomy slugs, and
every reader (gate, diversity, affinity, feed) therefore sees one vocabulary.

Field-name / vocabulary contract (data-layer discipline):
  * ``POOL_SOURCE_CATEGORY_TO_TAXONOMY`` — the only place the source→taxonomy map
    lives. Writers and readers both go through ``to_taxonomy_slug``.
  * ``POOL_PRODUCIBLE_TAXONOMY_SLUGS`` — the taxonomy slugs the pool can actually
    satisfy today (derived from the map). The scoring gate intersects a user's
    declared/learned interests with this set; the onboarding picker only offers
    these so a user can never declare an interest no source can fill and get
    silently muted.
  * ``ONBOARDABLE_CATEGORIES`` — the producible slugs, ordered for the picker.
  * ``backend/tests/test_content_category_map.py`` enumerates every raw category
    any fetcher can emit and fails CI if one is unmapped (writer→reader contract).
"""

from __future__ import annotations

from ...lib.logger import logger
from ..user_aura_schema import INTEREST_CATEGORIES, OTHER_CATEGORY

# Source/fetcher category -> closed-taxonomy slug (user_aura_schema). The keys are
# every raw ``CandidateInput.category`` value a fetcher can produce; the values are
# all valid taxonomy slugs. A self-check below guarantees the values stay valid.
POOL_SOURCE_CATEGORY_TO_TAXONOMY: dict[str, str] = {
    "tech": "technology_computing",       # Hacker News + Google News TECHNOLOGY
    "research": "science_nature",         # arXiv
    "science": "science_nature",          # Google News SCIENCE
    "news": "news_current_affairs",       # Google News WORLD
    "business": "business_economy",       # Google News BUSINESS
    "sports": "sports",                   # cricket (RSS + live) + Google News SPORTS
    "entertainment": "entertainment_media",   # Google News ENTERTAINMENT (new)
    "health": "health_medical",           # Google News HEALTH (new)
    "nation": "regional_local_affairs",   # Google News NATION, per-locale (new)
}

# Fail fast at import time if anyone adds a mapping to a slug that is not part of
# the closed taxonomy — that would route candidates to a category no reader knows.
_INVALID_TARGETS = {
    slug for slug in POOL_SOURCE_CATEGORY_TO_TAXONOMY.values()
    if slug not in INTEREST_CATEGORIES
}
assert not _INVALID_TARGETS, (
    f"content_category_map: mapping targets are not valid taxonomy slugs: "
    f"{_INVALID_TARGETS}. Add them to user_aura_schema.CATEGORY_LABELS first."
)

# The taxonomy slugs the pool can satisfy today (what the gate intersects against
# and the onboarding picker offers). Derived from the map so it can never drift.
POOL_PRODUCIBLE_TAXONOMY_SLUGS: frozenset[str] = frozenset(
    POOL_SOURCE_CATEGORY_TO_TAXONOMY.values()
)

# The producible slugs, ordered for the onboarding multi-select. Ordered by broad
# appeal first so the picker reads naturally; the set membership is what matters,
# the order is cosmetic. Kept as a literal tuple (not sorted(set)) so the picker
# order is stable and reviewable, with a self-check that it equals the producible
# set so a future map change can't leave a producible category off the picker.
ONBOARDABLE_CATEGORIES: tuple[str, ...] = (
    "entertainment_media",
    "sports",
    "news_current_affairs",
    "technology_computing",
    "business_economy",
    "health_medical",
    "science_nature",
    "regional_local_affairs",
)
assert set(ONBOARDABLE_CATEGORIES) == POOL_PRODUCIBLE_TAXONOMY_SLUGS, (
    "content_category_map: ONBOARDABLE_CATEGORIES must list exactly the producible "
    "slugs — update it when POOL_SOURCE_CATEGORY_TO_TAXONOMY changes."
)


def to_taxonomy_slug(raw: str | None) -> str:
    """Normalise a raw source/fetcher category to a closed-taxonomy slug.

    Idempotent: a value that is ALREADY a valid taxonomy slug (including a slug
    that previously coerced to ``other``) is returned unchanged, so this is safe
    to call both at write time (on raw fetcher vocab) and at read time (on
    already-normalised candidates during the deploy-window transition).

    A genuinely unknown raw category is coerced to ``other`` and logged at ERROR
    (fail loud): it still enters the pool but matches no allow_set, so it can
    never be mis-sent, and the loud log means an unmapped fetcher category screams
    instead of silently shrinking the producible world.
    """
    value = (raw or "").strip()
    if value in POOL_SOURCE_CATEGORY_TO_TAXONOMY:
        return POOL_SOURCE_CATEGORY_TO_TAXONOMY[value]
    if value in INTEREST_CATEGORIES:
        # Already a taxonomy slug (normalised candidate, or 'other'). Idempotent.
        return value
    logger.error(
        "content_category_map: unmapped raw category coerced to 'other' — a "
        "fetcher emitted a category with no mapping; add it to "
        "POOL_SOURCE_CATEGORY_TO_TAXONOMY so the pool can satisfy it.",
        {"raw_category": value},
    )
    return OTHER_CATEGORY
