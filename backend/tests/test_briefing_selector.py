"""Tests for the briefing candidate selector — the buzzing-across-categories core.

``rank_and_diversify`` is pure (no Firestore), so the guarantees are unit-tested
directly: category spread via round-robin, the per-category cap, the item ceiling,
the substance floor, and that a user with NO vector still gets a full buzz-ranked set
(the cold-start case the old rank_session could not serve).
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.services.briefing.candidate_selector import (
    MIN_BODY_CHARS,
    rank_and_diversify,
)
from src.services.signal_engine.content_pool import ScoredCandidate

_NOW = datetime(2026, 6, 16, 6, 0, tzinfo=UTC)
_BODY = "x" * (MIN_BODY_CHARS + 10)


def _cand(cid: str, category: str, *, salience: float = 0.5, body: str = _BODY) -> ScoredCandidate:
    return ScoredCandidate(
        content_id=cid,
        source="google_news",
        category=category,
        title=f"t-{cid}",
        body=body,
        url=f"https://example.com/{cid}",
        embedding=[0.0] * 8,
        freshness_ts=_NOW,
        cosine_similarity=0.0,
        salience=salience,
    )


def test_round_robin_spreads_across_categories():
    # One hot category with many items must not crowd out the others.
    cands = [_cand(f"s{i}", "sports", salience=0.9) for i in range(6)]
    cands += [_cand("t1", "technology_ai", salience=0.4)]
    cands += [_cand("w1", "world", salience=0.3)]
    selected = rank_and_diversify(cands, None, _NOW, max_items=10, max_per_category=3)
    categories = {it.category for it in selected}
    assert categories == {"sports", "technology_ai", "world"}
    # Per-category cap holds even though sports had the 6 strongest items.
    assert sum(1 for it in selected if it.category == "sports") == 3


def test_respects_item_ceiling():
    cands = [_cand(f"c{i}", f"cat{i % 5}", salience=0.5) for i in range(40)]
    selected = rank_and_diversify(cands, None, _NOW, max_items=10, max_per_category=3)
    assert len(selected) == 10


def test_drops_thin_substance_items():
    cands = [
        _cand("good", "sports", body="y" * (MIN_BODY_CHARS + 1)),
        _cand("thin", "world", body="too short"),
    ]
    selected = rank_and_diversify(cands, None, _NOW, max_items=10, max_per_category=3)
    assert [it.content_id for it in selected] == ["good"]


def test_works_without_user_vector():
    # No vector → buzz + freshness decide; the strongest salience leads.
    cands = [
        _cand("low", "sports", salience=0.1),
        _cand("high", "world", salience=0.9),
    ]
    selected = rank_and_diversify(cands, None, _NOW, max_items=10, max_per_category=3)
    assert selected[0].content_id == "high"


def test_empty_pool_returns_empty():
    assert rank_and_diversify([], None, _NOW, max_items=10, max_per_category=3) == []
