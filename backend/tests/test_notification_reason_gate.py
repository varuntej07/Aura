"""
Regression coverage for the "every notification has a defensible reason" contract.

Two beta users received hollow, off-topic pushes ("A post about doing nothing at
work") because a bodyless, blanket-tagged Hacker News story passed the category gate
on a false label and the relevance check fail-OPEN rubber-stamped it. These tests pin
the three fixes:

  1. A push-ineligible candidate (e.g. a bodyless HN story) is dropped from the
     notification path entirely — it can never fire a push.
  2. The relevance gate is fail-CLOSED on a missing reason: an is_relevant=true
     verdict with no named relevance_reason does NOT send.
  3. A framer outage (the FRAMER_UNAVAILABLE_REASON sentinel) defers the send and
     logs a loud WARNING — an infra outage must never masquerade as "not relevant".
  4. A genuine send records the framer's relevance_reason on the outcome doc and the
     PostHog event, so every fired notification is auditable after the fact.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.signal_engine import feature_store, scoring_loop
from src.services.signal_engine.content_pool import ScoredCandidate
from src.services.signal_engine.notification_framer import FRAMER_UNAVAILABLE_REASON


def _candidate(*, push_eligible: bool = True) -> ScoredCandidate:
    return ScoredCandidate(
        content_id="gn_abc123",
        source="google_news",
        category="tech",
        title="A neat new compiler",
        body="A real article body with substance to assess.",
        url="https://example.com/a",
        embedding=[0.1, 0.2, 0.3],
        freshness_ts=datetime.now(UTC),
        cosine_similarity=0.9,
        sub_category="",
        push_eligible=push_eligible,
    )


def _ready_state() -> feature_store.SignalStoreState:
    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.user_vector = [0.1] * feature_store.USER_VECTOR_DIMENSION
    state.sends_today = 0
    state.consecutive_no_open_ticks = 0
    state.time_slot_open_rates = [1.0] * feature_store.TIME_SLOTS_PER_DAY
    return state


def _framer_result(*, is_relevant: bool, relevance_reason: str):
    return SimpleNamespace(
        title="t", body="b", opening_chat_message="hey, saw this",
        is_relevant=is_relevant, relevance_reason=relevance_reason,
        content_kind="read",
    )


@pytest.fixture
def patched(monkeypatch):
    """Stub the I/O around the real send path. Per-test we override the candidate
    list and the framer result; everything else reaches a would-be send."""
    monkeypatch.setattr(scoring_loop, "_load_user_doc", AsyncMock(return_value={"timezone": "UTC"}))
    monkeypatch.setattr(scoring_loop, "_read_user_aura", AsyncMock(return_value={}))
    monkeypatch.setattr(scoring_loop, "_sweep_timeouts", AsyncMock(return_value=0))
    monkeypatch.setattr(scoring_loop, "_should_refresh_user_vector", lambda state: False)
    monkeypatch.setattr(scoring_loop, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(
        scoring_loop, "_load_recent_outcome_categories", AsyncMock(return_value=[])
    )
    monkeypatch.setattr(
        scoring_loop, "_build_framing_context", lambda *a, **k: scoring_loop.UserFramingContext()
    )
    # Post-cutover the tick ENQUEUES via orchestrator.submit; a BLOCKED candidate means
    # submit is never awaited (the contract these gate tests pin). The outcome + funnel
    # recording moved to on_news_delivered (covered by the records-reason test below).
    submit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(scoring_loop.orchestrator, "submit", submit_mock)
    monkeypatch.setattr(feature_store, "write_outcome_pending", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", AsyncMock())
    return submit_mock


async def _run(monkeypatch, *, candidates):
    monkeypatch.setattr(
        scoring_loop, "find_nearest_for_user", AsyncMock(return_value=candidates)
    )
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=_ready_state())):
        await scoring_loop._score_one_user("uid", MagicMock(), summary)
    return summary


async def test_push_ineligible_candidate_never_sends(patched, monkeypatch):
    """A bodyless HN-style candidate (push_eligible=False) is dropped from the
    notification path even with a strong cosine — the framer is never even called."""
    framer = AsyncMock(return_value=_framer_result(is_relevant=True, relevance_reason="x"))
    monkeypatch.setattr(scoring_loop, "frame_notification", framer)

    summary = await _run(monkeypatch, candidates=[_candidate(push_eligible=False)])

    assert summary.notifications_sent == 0
    patched.assert_not_awaited()
    framer.assert_not_awaited()  # dropped before scoring, never framed


async def test_empty_reason_blocks_send(patched, monkeypatch):
    """Fail-closed: an is_relevant=true verdict with no named reason does NOT send."""
    monkeypatch.setattr(
        scoring_loop, "frame_notification",
        AsyncMock(return_value=_framer_result(is_relevant=True, relevance_reason="   ")),
    )
    summary = await _run(monkeypatch, candidates=[_candidate()])

    assert summary.notifications_sent == 0
    patched.assert_not_awaited()


async def test_framer_unavailable_defers_and_warns(patched, monkeypatch):
    """The framer-outage sentinel suppresses the send AND logs a loud WARNING, so a
    sustained outage is never mistaken for 'nothing was relevant'."""
    monkeypatch.setattr(
        scoring_loop, "frame_notification",
        AsyncMock(return_value=_framer_result(
            is_relevant=False, relevance_reason=FRAMER_UNAVAILABLE_REASON,
        )),
    )
    warn = MagicMock()
    monkeypatch.setattr(scoring_loop.logger, "warn", warn)

    summary = await _run(monkeypatch, candidates=[_candidate()])

    assert summary.notifications_sent == 0
    patched.assert_not_awaited()
    assert any("framer UNAVAILABLE" in str(c) for c in warn.call_args_list)


async def test_send_records_relevance_reason_on_outcome_and_event(patched, monkeypatch):
    """A genuine send threads the named reason into the proposal, then on delivery into
    the outcome doc and the funnel event, so every fired notification is auditable."""
    from src.services.notification_service import NotificationResult

    monkeypatch.setattr(
        scoring_loop, "frame_notification",
        AsyncMock(return_value=_framer_result(
            is_relevant=True, relevance_reason="names your compiler interest",
        )),
    )
    outcome_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(feature_store, "write_outcome_pending", outcome_mock)
    capture = AsyncMock()
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", capture)

    summary = await _run(monkeypatch, candidates=[_candidate()])

    # The tick enqueues the proposal carrying the reason on its decision.
    assert summary.notifications_sent == 1
    proposal = patched.await_args.args[0]
    assert proposal.decision.relevance_reason == "names your compiler interest"

    # On delivery, the hook records that reason on the outcome doc + the funnel event.
    with patch.object(feature_store, "read_state", AsyncMock(return_value=_ready_state())):
        await scoring_loop.on_news_delivered(
            proposal, NotificationResult(tokens_targeted=1, success_count=1, failure_count=0),
        )
    assert outcome_mock.await_args.kwargs["relevance_reason"] == "names your compiler interest"
    assert capture.await_args.kwargs["properties"]["relevance_reason"] == "names your compiler interest"
