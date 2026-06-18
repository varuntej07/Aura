"""
The one-vocabulary contract for the signal engine.

The content pool and the interest taxonomy MUST speak one category language. This
test is the writer->reader contract: it enumerates every raw category any fetcher
can emit and fails CI if one is unmapped, so a fetcher that adds a new category
breaks the build instead of silently shrinking the producible world (the exact
"zero rows looks healthy" failure this project has been bitten by).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.services.signal_engine import content_category_map as ccm
from src.services.user_aura_schema import INTEREST_CATEGORIES, OTHER_CATEGORY


def _every_raw_fetcher_category() -> set[str]:
    """Every raw CandidateInput.category value a fetcher can produce.

    google_news categories are pulled from the live module constants (so a new
    feed is covered automatically); newsdata maps its own category vocabulary onto
    the same source vocab, so those mapped values are pulled from the live newsdata
    map. content_ingest's per-source fallbacks ("news") are added explicitly.
    """
    from src.agents.data_fetchers import google_news, newsdata

    raw: set[str] = set()
    for _topic, category in google_news._TOPIC_FEEDS:
        raw.add(category)
    for _topic, category in google_news._GLOBAL_TOPIC_FEEDS:
        raw.add(category)
    # newsdata maps its categories onto this same source vocab before tagging a
    # candidate — every target value must be mappable too.
    raw.update(newsdata._CATEGORY_TO_SOURCE_VOCAB.values())
    # content_ingest._map_google_news / _map_newsdata fallback category.
    raw.add("news")
    return raw


def test_every_fetcher_category_maps_to_a_valid_taxonomy_slug():
    for raw in _every_raw_fetcher_category():
        assert raw in ccm.POOL_SOURCE_CATEGORY_TO_TAXONOMY, (
            f"fetcher category {raw!r} has no mapping in "
            "POOL_SOURCE_CATEGORY_TO_TAXONOMY — add it so the pool can satisfy it."
        )
        slug = ccm.to_taxonomy_slug(raw)
        assert slug in INTEREST_CATEGORIES
        assert slug != OTHER_CATEGORY  # a real fetcher category must not fall through


def test_unmapped_raw_coerces_to_other_and_logs_error(monkeypatch):
    error_log = MagicMock()
    monkeypatch.setattr(ccm.logger, "error", error_log)

    assert ccm.to_taxonomy_slug("weather_forecast") == OTHER_CATEGORY
    error_log.assert_called_once()  # fail loud, never silent


def test_to_taxonomy_slug_is_idempotent_on_taxonomy_slugs():
    # A value that is already a taxonomy slug returns unchanged (safe to re-run at
    # read time during the deploy-window transition without re-logging an error).
    for slug in ("technology_computing", "sports", OTHER_CATEGORY):
        assert ccm.to_taxonomy_slug(slug) == slug


def test_producible_and_onboardable_derive_from_the_map():
    assert ccm.POOL_PRODUCIBLE_TAXONOMY_SLUGS == frozenset(
        ccm.POOL_SOURCE_CATEGORY_TO_TAXONOMY.values()
    )
    assert set(ccm.ONBOARDABLE_CATEGORIES) == ccm.POOL_PRODUCIBLE_TAXONOMY_SLUGS
    # Every producible/onboardable slug is a real taxonomy slug.
    for slug in ccm.ONBOARDABLE_CATEGORIES:
        assert slug in INTEREST_CATEGORIES


async def test_add_candidates_normalises_category_on_write(monkeypatch):
    """A raw 'tech' candidate must be stored as the taxonomy slug, not raw vocab."""
    from src.services.signal_engine import content_pool

    monkeypatch.setattr(content_pool, "embed_texts", AsyncMock(return_value=[[0.1] * 768]))
    monkeypatch.setattr(content_pool, "_filter_existing_ids", AsyncMock(return_value=set()))

    written_docs: list[dict] = []

    class _Batch:
        def set(self, _ref, doc):
            written_docs.append(doc)

        def commit(self):
            return None

    db = MagicMock()
    db.batch.return_value = _Batch()
    monkeypatch.setattr(content_pool, "admin_firestore", lambda: db)

    item = content_pool.CandidateInput(
        source="hackernews", category="tech", title="A title", body="body", url="https://x/1"
    )
    written = await content_pool.add_candidates([item])

    assert written == 1
    assert written_docs[0]["category"] == "technology_computing"  # normalised, not "tech"
