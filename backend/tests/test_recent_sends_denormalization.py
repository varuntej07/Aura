"""
Coverage for the recent-sends denormalization: signal_store/state.recent_sends
replaces two per-tick range queries (already-sent content_ids, most-recent
outcome categories for the diversity tie-breaker) with an in-memory ring buffer
read alongside the rest of the state doc.

Contracts pinned:
  1. feature_store.record_recent_send appends + trims the ring buffer in place.
  2. The encode/decode round-trip preserves recent_sends and
     recent_sends_backfilled, and a pre-existing doc (field absent, exactly what
     every prod user's doc looks like before this shipped) decodes to the safe
     "not yet backfilled" default rather than looking like real empty history.
  3. scoring_loop._derive_recent_sends reproduces the old two-query semantics in
     memory: the content_id dedup set has no time window (row-count bounded
     only), the category list is most-recent-first, capped, and time-windowed.
  4. scoring_loop._ensure_recent_sends_backfilled performs the one-time backfill
     exactly once (no-op on a second call), joins content_pool only for legacy
     rows missing the (now-inline) category field, and self-heals a pre-existing
     user's state without ever double-fetching.
  5. on_news_delivered appends a real delivery to the ring buffer and threads
     category through to write_outcome_pending, so future backfills never need
     the join for sends made after this shipped.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from src.services.signal_engine import feature_store, scoring_loop
from src.services.signal_engine.content_pool import ScoredCandidate


def _entry(content_id: str, category: str, *, hours_ago: float = 0.0) -> dict:
    return {
        "content_id": content_id,
        "category": category,
        "sent_at": datetime.now(UTC) - timedelta(hours=hours_ago),
    }


# ── feature_store.record_recent_send ────────────────────────────────────────

def test_record_recent_send_appends():
    state = feature_store.SignalStoreState()
    feature_store.record_recent_send(
        state, content_id="c1", category="tech", sent_at=datetime.now(UTC), cap=60,
    )
    assert len(state.recent_sends) == 1
    assert state.recent_sends[0]["content_id"] == "c1"
    assert state.recent_sends[0]["category"] == "tech"


def test_record_recent_send_trims_oldest_first_when_over_cap():
    state = feature_store.SignalStoreState()
    state.recent_sends = [_entry(f"c{i}", "tech") for i in range(3)]
    feature_store.record_recent_send(
        state, content_id="new", category="sports", sent_at=datetime.now(UTC), cap=3,
    )
    ids = [e["content_id"] for e in state.recent_sends]
    assert ids == ["c1", "c2", "new"]  # c0 (oldest) dropped, cap held at 3


def test_record_recent_send_ignores_empty_content_id():
    state = feature_store.SignalStoreState()
    feature_store.record_recent_send(
        state, content_id="", category="tech", sent_at=datetime.now(UTC), cap=60,
    )
    assert state.recent_sends == []


# ── feature_store encode/decode round-trip ──────────────────────────────────

def test_encode_decode_round_trips_recent_sends():
    state = feature_store.SignalStoreState()
    state.recent_sends = [_entry("c1", "tech"), _entry("c2", "sports")]
    state.recent_sends_backfilled = True

    decoded = feature_store._decode_state(feature_store._encode_state(state))

    assert decoded.recent_sends_backfilled is True
    assert [e["content_id"] for e in decoded.recent_sends] == ["c1", "c2"]
    assert [e["category"] for e in decoded.recent_sends] == ["tech", "sports"]


def test_missing_field_decodes_to_not_yet_backfilled():
    """The exact shape of every prod user's doc before this shipped: no
    recent_sends / recent_sends_backfilled key at all. Must decode to the
    "needs backfill" state, never to "genuinely empty history" — those look
    identical unless the flag distinguishes them."""
    decoded = feature_store._decode_state({"sends_today": 3})

    assert decoded.recent_sends == []
    assert decoded.recent_sends_backfilled is False


def test_decode_drops_malformed_entries_defensively():
    raw = {
        "recent_sends": [
            {"content_id": "c1", "category": "tech", "sent_at": None},  # ok, no timestamp
            {"category": "sports"},  # missing content_id -> dropped
            "not-a-dict",  # dropped
            {"content_id": "c2"},  # missing category -> defaults to ""
        ],
    }
    decoded = feature_store._decode_state(raw)
    assert [e["content_id"] for e in decoded.recent_sends] == ["c1", "c2"]
    assert decoded.recent_sends[1]["category"] == ""


# ── scoring_loop._derive_recent_sends ───────────────────────────────────────

def test_derive_recent_sends_empty_state():
    ids, cats = scoring_loop._derive_recent_sends(feature_store.SignalStoreState())
    assert ids == set()
    assert cats == []


def test_derive_recent_sends_content_id_set_has_no_time_window():
    """Unlike categories, the dedup set is bounded only by ring-buffer size (the
    old _load_recent_sent_content_ids query had a row-count limit, never a time
    filter) — an entry outside DIVERSITY_LOOKBACK_HOURS still counts for dedup."""
    state = feature_store.SignalStoreState()
    state.recent_sends = [_entry("old", "tech", hours_ago=48)]
    ids, cats = scoring_loop._derive_recent_sends(state)
    assert ids == {"old"}
    assert cats == []  # too old for the diversity window


def test_derive_recent_sends_categories_most_recent_first_and_capped():
    state = feature_store.SignalStoreState()
    # Oldest-first storage order, as record_recent_send appends.
    state.recent_sends = [
        _entry("c1", "a", hours_ago=5),
        _entry("c2", "b", hours_ago=4),
        _entry("c3", "c", hours_ago=3),
        _entry("c4", "d", hours_ago=2),
        _entry("c5", "e", hours_ago=1),
        _entry("c6", "f", hours_ago=0.5),  # 6th entry, should be excluded by the cap
    ]
    _, cats = scoring_loop._derive_recent_sends(state)
    assert cats == ["f", "e", "d", "c", "b"]  # newest-first, capped at RECENT_OUTCOMES_FOR_DIVERSITY=5


def test_derive_recent_sends_stops_at_first_stale_entry():
    state = feature_store.SignalStoreState()
    state.recent_sends = [
        _entry("stale", "old_cat", hours_ago=30),   # outside 24h window
        _entry("fresh", "new_cat", hours_ago=1),
    ]
    _, cats = scoring_loop._derive_recent_sends(state)
    assert cats == ["new_cat"]  # walk stops at the stale entry, never reaches past it


# ── scoring_loop._ensure_recent_sends_backfilled ────────────────────────────

@pytest.mark.asyncio
async def test_backfill_noop_when_already_backfilled():
    state = feature_store.SignalStoreState()
    state.recent_sends_backfilled = True
    fetch_mock = AsyncMock()
    with patch.object(scoring_loop, "_load_recent_sent_rows", fetch_mock):
        await scoring_loop._ensure_recent_sends_backfilled("uid", state)
    fetch_mock.assert_not_called()


@pytest.mark.asyncio
async def test_backfill_populates_and_sets_flag():
    state = feature_store.SignalStoreState()
    rows = [_entry("c1", "tech", hours_ago=2), _entry("c2", "sports", hours_ago=1)]
    with patch.object(scoring_loop, "_load_recent_sent_rows", AsyncMock(return_value=rows)):
        await scoring_loop._ensure_recent_sends_backfilled("uid", state)

    assert state.recent_sends_backfilled is True
    assert [e["content_id"] for e in state.recent_sends] == ["c1", "c2"]


@pytest.mark.asyncio
async def test_backfill_joins_content_pool_only_for_rows_missing_category():
    """A legacy row (written before write_outcome_pending stored category inline)
    has category="" from _load_recent_sent_rows; a post-migration row already has
    it. Only the legacy one should trigger a get_candidate join."""
    state = feature_store.SignalStoreState()
    legacy_row = {"content_id": "legacy", "category": "", "sent_at": datetime.now(UTC)}
    modern_row = {"content_id": "modern", "category": "sports", "sent_at": datetime.now(UTC)}

    cand = ScoredCandidate(
        content_id="legacy", source="google_news", category="tech",
        title="t", body="b", url="https://example.com", embedding=[0.1],
        freshness_ts=datetime.now(UTC), cosine_similarity=0.0,
    )
    get_candidate_mock = AsyncMock(return_value=cand)
    with patch.object(scoring_loop, "_load_recent_sent_rows", AsyncMock(return_value=[legacy_row, modern_row])):
        with patch("src.services.signal_engine.content_pool.get_candidate", get_candidate_mock):
            await scoring_loop._ensure_recent_sends_backfilled("uid", state)

    get_candidate_mock.assert_awaited_once_with("legacy")
    by_id = {e["content_id"]: e["category"] for e in state.recent_sends}
    assert by_id["legacy"] == "tech"  # filled in via the join
    assert by_id["modern"] == "sports"  # untouched, already had it


# ── on_news_delivered appends to the ring buffer ────────────────────────────

@pytest.mark.asyncio
async def test_on_news_delivered_appends_recent_send_and_threads_category(monkeypatch):
    from src.services.notification_service import NotificationResult
    from src.services.notifications.proposal import NotificationProposal, ProposalKind, SOURCE_NEWS

    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    outcome_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(feature_store, "write_outcome_pending", outcome_mock)
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", AsyncMock())

    state = feature_store.SignalStoreState()
    proposal = NotificationProposal(
        user_id="u1", source=SOURCE_NEWS, kind=ProposalKind.PROACTIVE,
        dedup_key="c1", title="t", body="b",
        data={"content_id": "c1", "notification_id": "n1", "category": "tech"},
        decision=scoring_loop.NotificationDecision(score=0.9, relevance_reason="big"),
    )

    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop.on_news_delivered(
            proposal, NotificationResult(tokens_targeted=1, success_count=1, failure_count=0),
        )

    assert len(state.recent_sends) == 1
    assert state.recent_sends[0]["content_id"] == "c1"
    assert state.recent_sends[0]["category"] == "tech"
    outcome_mock.assert_awaited_once()
    assert outcome_mock.await_args.kwargs["category"] == "tech"
