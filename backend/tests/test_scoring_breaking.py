"""
Coverage for the breaking lane (Lane B) and already-sent suppression in the
scoring loop.

Contracts pinned:
  1. A high-salience story reaches a brand-new user (empty vector, outside any
     declared interest), bypassing the personal gate, tagged lane=breaking and
     content_kind=discuss, and counted in both daily counters.
  2. Breaking is hard-capped at MAX_BREAKING_SENDS_PER_DAY — a user who already
     got today's breaking push gets no second one.
  3. The personal lane never re-sends a content_id the user was already sent.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.signal_engine import feature_store, scoring_loop
from src.services.signal_engine.content_pool import ScoredCandidate


def _breaking_candidate() -> ScoredCandidate:
    return ScoredCandidate(
        content_id="gn_break", source="google_news", category="news",
        title="A worldwide event", body="It is enormous.",
        url="https://example.com/break", embedding=[0.1, 0.2, 0.3],
        freshness_ts=datetime.now(UTC), cosine_similarity=0.0, salience=0.95,
    )


def _personal_candidate() -> ScoredCandidate:
    return ScoredCandidate(
        content_id="gn_seen", source="google_news", category="tech",
        title="A neat new compiler", body="It does interesting things.",
        url="https://example.com/a", embedding=[0.1, 0.2, 0.3],
        freshness_ts=datetime.now(UTC), cosine_similarity=0.9, salience=0.0,
    )


def _stub_common(monkeypatch):
    monkeypatch.setattr(scoring_loop, "_load_user_doc", AsyncMock(return_value={"timezone": "UTC"}))
    monkeypatch.setattr(scoring_loop, "_sweep_timeouts", AsyncMock(return_value=0))
    monkeypatch.setattr(scoring_loop, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(scoring_loop, "_read_user_aura", AsyncMock(return_value={}))
    monkeypatch.setattr(scoring_loop, "_load_recent_outcome_categories", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        scoring_loop, "_build_framing_context",
        lambda *a, **k: scoring_loop.UserFramingContext(),
    )
    monkeypatch.setattr(feature_store, "write_outcome_pending", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", AsyncMock())
    monkeypatch.setattr(
        scoring_loop, "try_claim_proactive_slot",
        AsyncMock(return_value=SimpleNamespace(allowed=True, reason=None)),
    )


async def test_breaking_reaches_fresh_user_outside_interests(monkeypatch):
    _stub_common(monkeypatch)
    monkeypatch.setattr(scoring_loop, "_load_recent_sent_content_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(
        scoring_loop, "list_recent_breaking_candidates",
        AsyncMock(return_value=[_breaking_candidate()]),
    )
    monkeypatch.setattr(scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        scoring_loop, "frame_notification",
        AsyncMock(return_value=SimpleNamespace(
            title="t", body="b", opening_chat_message="o",
            is_relevant=True, relevance_reason="globally significant breaking news",
            content_kind="discuss",
        )),
    )
    send_mock = AsyncMock(return_value=SimpleNamespace(delivered=True))
    monkeypatch.setattr(scoring_loop, "send_notification", send_mock)

    # Fresh user: zero vector, not yet bootstrapped — the personal lane would skip,
    # but breaking runs first and is vector-independent.
    state = feature_store.SignalStoreState()
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop._score_one_user("uid-fresh", MagicMock(), summary)

    assert summary.notifications_sent == 1
    send_mock.assert_awaited_once()
    data = send_mock.await_args.kwargs["data"]
    assert data["lane"] == "breaking"
    assert data["notification_origin"] == "signal_engine"
    assert data["content_kind"] == "discuss"
    assert data["url"] == "https://example.com/break"  # citation rides along
    assert state.breaking_sends_today == 1
    assert state.sends_today == 1


async def test_breaking_capped_once_per_day(monkeypatch):
    _stub_common(monkeypatch)
    monkeypatch.setattr(scoring_loop, "_load_recent_sent_content_ids", AsyncMock(return_value=set()))
    breaking_query = AsyncMock(return_value=[_breaking_candidate()])
    monkeypatch.setattr(scoring_loop, "list_recent_breaking_candidates", breaking_query)
    monkeypatch.setattr(scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[]))
    send_mock = AsyncMock(return_value=SimpleNamespace(delivered=True))
    monkeypatch.setattr(scoring_loop, "send_notification", send_mock)

    # Already used today's breaking slot; vector empty + bootstrapped so the personal
    # lane simply skips. No second breaking send. sends_today_date must be today's
    # local date or the per-day reset would wipe breaking_sends_today back to 0.
    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.breaking_sends_today = 1
    state.sends_today_date = datetime.now(UTC).date().isoformat()
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop._score_one_user("uid-capped", MagicMock(), summary)

    assert summary.notifications_sent == 0
    send_mock.assert_not_awaited()
    breaking_query.assert_not_called()  # gate short-circuits before querying


async def test_personal_lane_suppresses_already_sent_story(monkeypatch):
    _stub_common(monkeypatch)
    # No breaking candidate this tick; the one personal candidate was already sent.
    monkeypatch.setattr(scoring_loop, "list_recent_breaking_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        scoring_loop, "_load_recent_sent_content_ids", AsyncMock(return_value={"gn_seen"})
    )
    monkeypatch.setattr(
        scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[_personal_candidate()])
    )
    send_mock = AsyncMock(return_value=SimpleNamespace(delivered=True))
    monkeypatch.setattr(scoring_loop, "send_notification", send_mock)

    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.user_vector = [0.1] * feature_store.USER_VECTOR_DIMENSION
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop._score_one_user("uid-dup", MagicMock(), summary)

    assert summary.notifications_sent == 0  # only candidate was suppressed
    send_mock.assert_not_awaited()
