"""
Behavioral coverage for the scoring loop's re-engagement funnel instrumentation.

Two contracts are pinned here:
  1. A successful send emits the top-of-funnel `signal_notification_sent` event
     with the exact join keys the client tap event reuses — if these drift, the
     PostHog funnel silently flattens (the "zero rows looks healthy" trap).
  2. Sending notifications while PostHog is unconfigured logs a loud WARNING, so
     a funnel-blind production tick is never mistaken for a healthy one.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.signal_engine import feature_store, scoring_loop
from src.services.signal_engine.content_pool import ScoredCandidate
from src.services.analytics.funnel_events import (
    EVENT_NOTIFICATION_SENT,
    NOTIFICATION_ORIGIN_SIGNAL_ENGINE,
    PROP_CATEGORY,
    PROP_CONTENT_ID,
    PROP_NOTIFICATION_ID,
    PROP_NOTIFICATION_ORIGIN,
)


def _sendable_candidate() -> ScoredCandidate:
    return ScoredCandidate(
        content_id="hn_abc123",
        source="hackernews",
        category="tech",
        title="A neat new compiler",
        body="It does interesting things.",
        url="https://example.com/a",
        embedding=[0.1, 0.2, 0.3],
        freshness_ts=datetime.now(UTC),
        cosine_similarity=0.9,
        sub_category="compilers",
    )


def _ready_state() -> feature_store.SignalStoreState:
    state = feature_store.SignalStoreState()
    # bootstrap_done plus a non-zero user_vector so the "no signal yet" early-return
    # is skipped (the loop skips any user whose vector is still all-zero, since a
    # zero query vector has no meaningful nearest neighbours), and a clean daily
    # counter so the send isn't capped.
    state.bootstrap_done = True
    state.user_vector = [0.1] * feature_store.USER_VECTOR_DIMENSION
    state.sends_today = 0
    state.consecutive_no_open_ticks = 0
    return state


@pytest.fixture
def patched_send_path(monkeypatch):
    """Stub every dependency of _score_one_user so it reaches a successful send,
    leaving the funnel capture as the only behavior under test."""
    cand = _sendable_candidate()

    monkeypatch.setattr(
        scoring_loop, "_load_user_timezone", AsyncMock(return_value="UTC")
    )
    monkeypatch.setattr(scoring_loop, "_sweep_timeouts", AsyncMock(return_value=0))
    monkeypatch.setattr(scoring_loop, "_should_refresh_user_vector", lambda state: False)
    # Pin active hours so this funnel test never flakes at night UTC.
    monkeypatch.setattr(scoring_loop, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(
        scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[cand])
    )
    monkeypatch.setattr(
        scoring_loop, "_load_recent_outcome_categories", AsyncMock(return_value=[])
    )
    # Force the send decision so the test doesn't depend on scoring math.
    monkeypatch.setattr(scoring_loop, "is_sendable", lambda *a, **k: (True, None))
    monkeypatch.setattr(
        scoring_loop,
        "_build_framing_context",
        AsyncMock(return_value=scoring_loop.UserFramingContext()),
    )
    monkeypatch.setattr(
        scoring_loop,
        "frame_notification",
        AsyncMock(
            return_value=SimpleNamespace(
                title="t", body="b", opening_chat_message="hey, saw this"
            )
        ),
    )
    monkeypatch.setattr(
        scoring_loop,
        "send_notification",
        AsyncMock(return_value=SimpleNamespace(delivered=True)),
    )
    monkeypatch.setattr(
        feature_store, "write_outcome_pending", AsyncMock(return_value=None)
    )
    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    return cand


async def test_successful_send_emits_funnel_event_with_join_keys(
    patched_send_path, monkeypatch
):
    cand = patched_send_path
    capture = AsyncMock()
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", capture)

    summary = scoring_loop.TickSummary()
    models = MagicMock()
    with patch.object(
        feature_store, "read_state", AsyncMock(return_value=_ready_state())
    ):
        await scoring_loop._score_one_user("uid-42", models, summary)

    assert summary.notifications_sent == 1
    capture.assert_awaited_once()
    kwargs = capture.await_args.kwargs
    assert kwargs["distinct_id"] == "uid-42"
    assert kwargs["event"] == EVENT_NOTIFICATION_SENT
    props = kwargs["properties"]
    assert props[PROP_CONTENT_ID] == cand.content_id
    assert props[PROP_CATEGORY] == cand.category
    assert props[PROP_NOTIFICATION_ORIGIN] == NOTIFICATION_ORIGIN_SIGNAL_ENGINE
    # notification_id is a fresh uuid per send; assert it's present and non-empty
    # so the sent->tapped join key always exists.
    assert props[PROP_NOTIFICATION_ID]


async def test_run_tick_warns_when_sends_happen_but_posthog_unconfigured(monkeypatch):
    monkeypatch.setattr(
        scoring_loop.feature_store,
        "list_active_user_ids",
        AsyncMock(return_value=["uid-1"]),
    )

    async def _fake_score(user_id, models, summary):
        summary.notifications_sent += 1

    monkeypatch.setattr(scoring_loop, "_score_one_user", _fake_score)
    monkeypatch.setattr(scoring_loop, "get_model_provider", lambda: MagicMock())
    monkeypatch.setattr(scoring_loop.posthog_client, "flush", AsyncMock())
    monkeypatch.setattr(scoring_loop.settings, "POSTHOG_API_KEY", "")  # unconfigured

    with patch.object(scoring_loop.logger, "warn") as mock_warn:
        summary = await scoring_loop.run_tick()

    assert summary.notifications_sent == 1
    blind_warnings = [
        c for c in mock_warn.call_args_list if "funnel is blind" in str(c)
    ]
    assert len(blind_warnings) == 1


async def test_run_tick_no_warn_when_posthog_configured(monkeypatch):
    monkeypatch.setattr(
        scoring_loop.feature_store,
        "list_active_user_ids",
        AsyncMock(return_value=["uid-1"]),
    )

    async def _fake_score(user_id, models, summary):
        summary.notifications_sent += 1

    monkeypatch.setattr(scoring_loop, "_score_one_user", _fake_score)
    monkeypatch.setattr(scoring_loop, "get_model_provider", lambda: MagicMock())
    monkeypatch.setattr(scoring_loop.posthog_client, "flush", AsyncMock())
    monkeypatch.setattr(scoring_loop.settings, "POSTHOG_API_KEY", "phc_live_key")

    with patch.object(scoring_loop.logger, "warn") as mock_warn:
        await scoring_loop.run_tick()

    blind_warnings = [
        c for c in mock_warn.call_args_list if "funnel is blind" in str(c)
    ]
    assert blind_warnings == []
