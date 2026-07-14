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
    """Stub the scoring tick's I/O. Post-cutover the tick ENQUEUES via orchestrator.submit
    (the real send + counter bumps + outcome happen later in the drain / on_news_delivered),
    so we patch submit and return that mock. The unified budget is no longer claimed here."""
    monkeypatch.setattr(scoring_loop, "_load_user_doc", AsyncMock(return_value={"timezone": "UTC"}))
    monkeypatch.setattr(scoring_loop, "_sweep_timeouts", AsyncMock(return_value=0))
    monkeypatch.setattr(scoring_loop, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(scoring_loop, "_read_user_aura", AsyncMock(return_value={}))
    monkeypatch.setattr(
        scoring_loop, "_build_framing_context",
        lambda *a, **k: scoring_loop.UserFramingContext(),
    )
    monkeypatch.setattr(feature_store, "write_outcome_pending", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", AsyncMock())
    submit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(scoring_loop.orchestrator, "submit", submit_mock)
    return submit_mock


async def test_breaking_reaches_fresh_user_outside_interests(monkeypatch):
    submit_mock = _stub_common(monkeypatch)
    # run_tick now fetches breaking candidates ONCE per tick and passes them into
    # _score_one_user, rather than _try_send_breaking querying per-user — so the
    # candidate list is supplied directly here instead of via a monkeypatched query.
    breaking_candidates = [_breaking_candidate()]
    monkeypatch.setattr(scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[]))
    monkeypatch.setattr(
        scoring_loop, "frame_notification",
        AsyncMock(return_value=SimpleNamespace(
            title="t", body="b", opening_chat_message="o",
            is_relevant=True, relevance_reason="globally significant breaking news",
            content_kind="discuss",
        )),
    )

    # Fresh user: zero vector, not yet bootstrapped — the personal lane would skip,
    # but breaking runs first and is vector-independent. recent_sends_backfilled=True
    # so the tick doesn't try a real backfill query against no mocked Firestore.
    state = feature_store.SignalStoreState()
    state.recent_sends_backfilled = True
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop._score_one_user("uid-fresh", MagicMock(), summary, breaking_candidates)

    assert summary.notifications_sent == 1  # enqueued
    submit_mock.assert_awaited_once()
    proposal = submit_mock.await_args.args[0]
    assert proposal.source == scoring_loop.SOURCE_NEWS
    assert proposal.kind == scoring_loop.ProposalKind.PROACTIVE
    data = proposal.data
    assert data["lane"] == "breaking"
    assert data["notification_origin"] == "signal_engine"
    assert data["content_kind"] == "discuss"
    assert data["url"] == "https://example.com/break"  # citation rides along
    # The freshest article timestamp drives the freshness gate.
    assert proposal.content_timestamp is not None
    # The daily counters now bump on DELIVERY (on_news_delivered), not on enqueue.


async def test_breaking_capped_once_per_day(monkeypatch):
    submit_mock = _stub_common(monkeypatch)
    breaking_candidates = [_breaking_candidate()]
    monkeypatch.setattr(scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[]))

    # Breaking quota already exhausted for today (the per-day breaking cap is
    # MAX_BREAKING_SENDS_PER_DAY; uncapped to a high number during beta, so reference the
    # constant rather than a literal). Vector empty + bootstrapped so the personal lane
    # simply skips. No second breaking send. sends_today_date must be today's local date
    # or the per-day reset would wipe breaking_sends_today back to 0.
    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.recent_sends_backfilled = True
    state.breaking_sends_today = scoring_loop.MAX_BREAKING_SENDS_PER_DAY
    state.sends_today_date = datetime.now(UTC).date().isoformat()
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop._score_one_user("uid-capped", MagicMock(), summary, breaking_candidates)

    assert summary.notifications_sent == 0
    submit_mock.assert_not_awaited()  # gate short-circuits before ever trying to send


async def test_personal_lane_suppresses_already_sent_story(monkeypatch):
    submit_mock = _stub_common(monkeypatch)
    # No breaking candidate this tick; the one personal candidate was already sent.
    breaking_candidates: list[ScoredCandidate] = []
    monkeypatch.setattr(
        scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[_personal_candidate()])
    )

    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.recent_sends_backfilled = True
    state.recent_sends = [{"content_id": "gn_seen", "category": "tech", "sent_at": datetime.now(UTC)}]
    state.user_vector = [0.1] * feature_store.USER_VECTOR_DIMENSION
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop._score_one_user("uid-dup", MagicMock(), summary, breaking_candidates)

    assert summary.notifications_sent == 0  # only candidate was suppressed
    submit_mock.assert_not_awaited()


# ── on_news_delivered: the delivery-dependent bookkeeping moved here from the tick ──
def _news_proposal(*, lane: str | None = None):
    from src.services.notifications.proposal import (
        SOURCE_NEWS,
        NotificationProposal,
        ProposalKind,
    )
    data = {"content_id": "c1", "notification_id": "n1", "category": "news",
            "sub_category": "world", "source": "google_news"}
    if lane:
        data["lane"] = lane
    return NotificationProposal(
        user_id="u1", source=SOURCE_NEWS, kind=ProposalKind.PROACTIVE,
        dedup_key="c1", title="t", body="b", data=data,
        decision=scoring_loop.NotificationDecision(score=0.9, relevance_reason="big"),
    )


async def test_on_news_delivered_breaking_bumps_counters_outcome_and_funnel(monkeypatch):
    from src.services.notification_service import NotificationResult

    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    outcome = AsyncMock(return_value=None)
    monkeypatch.setattr(feature_store, "write_outcome_pending", outcome)
    capture = AsyncMock()
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", capture)

    state = feature_store.SignalStoreState()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop.on_news_delivered(
            _news_proposal(lane="breaking"),
            NotificationResult(tokens_targeted=1, success_count=1, failure_count=0),
        )

    assert state.sends_today == 1
    assert state.breaking_sends_today == 1  # breaking lane bumps both counters
    outcome.assert_awaited_once()
    capture.assert_awaited_once()
    assert capture.await_args.kwargs["properties"]["lane"] == "breaking"


async def test_on_news_delivered_no_delivery_only_bumps_no_open(monkeypatch):
    from src.services.notification_service import NotificationResult

    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    outcome = AsyncMock(return_value=None)
    monkeypatch.setattr(feature_store, "write_outcome_pending", outcome)

    state = feature_store.SignalStoreState()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop.on_news_delivered(
            _news_proposal(),
            NotificationResult(tokens_targeted=1, success_count=0, failure_count=1),
        )

    assert state.sends_today == 0  # nothing delivered → no send counted
    assert state.consecutive_no_open_ticks == 1
    outcome.assert_not_awaited()  # no learning outcome for a non-delivery
