"""Coverage for the users_skipped_no_candidates counter.

When a user HAS a vector but find_nearest returns [] (pool starved or vector search
failing), the scoring loop used to return silently — invisible in every metric, which
is what sent the 2026-06-14 diagnosis chasing the vector index. This pins that the
fall-out is now counted, so the tick-health line and the 0-send warning name the real
cause instead of guessing."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

from src.services.signal_engine import feature_store, scoring_loop


def _ready_state() -> feature_store.SignalStoreState:
    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.user_vector = [0.1] * feature_store.USER_VECTOR_DIMENSION
    return state


async def test_empty_find_nearest_increments_no_candidates_counter(monkeypatch):
    # Stub every seam so the user reaches the personal lane, where find_nearest is [].
    monkeypatch.setattr(scoring_loop, "_load_user_doc", AsyncMock(return_value={"timezone": "UTC"}))
    monkeypatch.setattr(scoring_loop, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(scoring_loop, "_sweep_timeouts", AsyncMock(return_value=0))
    monkeypatch.setattr(scoring_loop, "_load_recent_sent_content_ids", AsyncMock(return_value=set()))
    monkeypatch.setattr(scoring_loop, "list_recent_breaking_candidates", AsyncMock(return_value=[]))
    monkeypatch.setattr(scoring_loop, "_should_refresh_user_vector", lambda state: False)
    monkeypatch.setattr(scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[]))
    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))

    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=_ready_state())):
        await scoring_loop._score_one_user("uid", MagicMock(), summary)

    assert summary.users_skipped_no_candidates == 1
    assert summary.notifications_sent == 0
    # It must NOT be miscounted as a no-signal skip (different root cause, different fix).
    assert summary.users_skipped_no_state == 0
