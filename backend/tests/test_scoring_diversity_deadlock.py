"""
Regression coverage for the diversity-penalty deadlock (root-caused 2026-06-09).

Background: notifications silently stopped for ~4 weeks. The content pool and every
user vector are tech-dominant, so the single nearest candidate each tick is almost
always category "tech". The diversity penalty multiplied the *gating* score by 0.6
whenever the top candidate's category matched the most recent outcome's category.
After the first tech send wrote a tech outcome row, every later tech candidate got
0.6x, dropping ~0.64 under the 0.45 threshold permanently — a self-reinforcing
deadlock that blocked all sends after the first per user.

The fix makes diversity a TIE-BREAKER, not a gate: sendability is decided on the
base score (diversity excluded), and diversity only orders the choice among
candidates that already clear the bar. These tests pin that contract.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.signal_engine import feature_store, scoring_loop
from src.services.signal_engine.content_pool import ScoredCandidate


def _candidate(category: str, cosine: float) -> ScoredCandidate:
    return ScoredCandidate(
        content_id=f"hn_{category}_{int(cosine * 100)}",
        source="hackernews",
        category=category,
        title="A neat new compiler",
        body="It does interesting things.",
        url="https://example.com/a",
        embedding=[0.1, 0.2, 0.3],
        freshness_ts=datetime.now(UTC),  # fresh -> freshness ~1.0
        cosine_similarity=cosine,
        sub_category="",
    )


def _ready_state() -> feature_store.SignalStoreState:
    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.user_vector = [0.1] * feature_store.USER_VECTOR_DIMENSION
    state.sends_today = 0
    state.consecutive_no_open_ticks = 0
    # Uniform open-rate history => time_slot_open_score returns a flat 1.0 instead
    # of the hour-dependent cold-start prior, so base score == cosine and the test
    # never flakes on the wall-clock hour.
    state.time_slot_open_rates = [1.0] * feature_store.TIME_SLOTS_PER_DAY
    return state


@pytest.fixture
def patched_scoring_path(monkeypatch):
    """Stub everything around the real scoring math so is_sendable / diversity run
    for real. The candidate list and recent-category list are set per test."""
    monkeypatch.setattr(scoring_loop, "_load_user_doc", AsyncMock(return_value={"timezone": "UTC"}))
    monkeypatch.setattr(scoring_loop, "_read_user_aura", AsyncMock(return_value={}))
    monkeypatch.setattr(scoring_loop, "_sweep_timeouts", AsyncMock(return_value=0))
    monkeypatch.setattr(scoring_loop, "_should_refresh_user_vector", lambda state: False)
    monkeypatch.setattr(scoring_loop, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(
        scoring_loop,
        "_build_framing_context",
        lambda *a, **k: scoring_loop.UserFramingContext(),
    )
    monkeypatch.setattr(
        scoring_loop,
        "frame_notification",
        AsyncMock(return_value=SimpleNamespace(
            title="t", body="b", opening_chat_message="hey",
            is_relevant=True, relevance_reason="matches your tech interest",
            content_kind="discuss",
        )),
    )
    # Post-cutover the scoring tick ENQUEUES via orchestrator.submit; notifications_sent
    # counts the enqueue, so these diversity/threshold assertions are unchanged.
    submit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(scoring_loop.orchestrator, "submit", submit_mock)
    monkeypatch.setattr(feature_store, "write_outcome_pending", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", AsyncMock())
    return submit_mock


async def test_same_category_recent_send_does_not_block(patched_scoring_path, monkeypatch):
    """A strong candidate whose category matches a recent send (diversity 0.6) must
    STILL send. Under the old gating logic 0.6 * 0.6 = 0.36 < 0.45 blocked it — that
    was the deadlock."""
    send_mock = patched_scoring_path
    cand = _candidate("tech", cosine=0.6)  # base ~0.6, clears 0.45 on its own
    monkeypatch.setattr(scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[cand]))
    # Most recent outcome is the same category -> diversity_penalty returns 0.6.
    monkeypatch.setattr(
        scoring_loop, "_load_recent_outcome_categories", AsyncMock(return_value=["tech"])
    )

    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=_ready_state())):
        await scoring_loop._score_one_user("uid-1", MagicMock(), summary)

    assert summary.notifications_sent == 1
    assert summary.blocked_below_threshold == 0
    send_mock.assert_awaited_once()


async def test_weak_candidate_still_blocked(patched_scoring_path, monkeypatch):
    """Diversity is only a tie-breaker now, so the threshold must still block a
    genuinely weak match (base well under 0.45) — the gate is not disabled."""
    send_mock = patched_scoring_path
    cand = _candidate("tech", cosine=0.30)  # base ~0.30, below threshold
    monkeypatch.setattr(scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[cand]))
    monkeypatch.setattr(
        scoring_loop, "_load_recent_outcome_categories", AsyncMock(return_value=[])
    )

    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=_ready_state())):
        await scoring_loop._score_one_user("uid-2", MagicMock(), summary)

    assert summary.notifications_sent == 0
    assert summary.blocked_below_threshold == 1
    assert summary.blocked_below_threshold_scores  # recorded for the health line
    send_mock.assert_not_awaited()


async def test_diversity_prefers_fresh_category_among_sendable(patched_scoring_path, monkeypatch):
    """When two candidates both clear the bar, diversity steers the pick toward the
    category NOT recently sent — without ever blocking the send."""
    send_mock = patched_scoring_path
    tech = _candidate("tech", cosine=0.62)      # slightly higher base
    sports = _candidate("sports", cosine=0.60)  # slightly lower base, fresh category
    monkeypatch.setattr(
        scoring_loop, "find_nearest_for_user", AsyncMock(return_value=[tech, sports])
    )
    # "tech" was just sent -> tech gets 0.6x in the tie-break, so sports should win
    # even though tech's base is marginally higher.
    monkeypatch.setattr(
        scoring_loop, "_load_recent_outcome_categories", AsyncMock(return_value=["tech"])
    )

    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=_ready_state())):
        await scoring_loop._score_one_user("uid-3", MagicMock(), summary)

    assert summary.notifications_sent == 1
    # The chosen candidate now rides in the enqueued proposal (submit's first arg).
    proposal = send_mock.await_args.args[0]
    assert proposal.data["category"] == "sports"


def test_exploration_picks_highest_base_in_target_category():
    """Exploration drift must send the STRONGEST off-affinity story, independent of
    the order of the scored list. The scoring loop no longer pre-sorts that list, so
    a "first match in iteration order" pick would silently send a weaker candidate.
    Here the weaker sports item is listed before the stronger one on purpose."""
    state = feature_store.SignalStoreState()
    # sports has the lowest affinity -> it is the exploration target category.
    state.category_affinities = {"tech": 1.0, "sports": 0.0}

    weak_sports = _candidate("sports", cosine=0.50)
    strong_sports = _candidate("sports", cosine=0.70)
    tech = _candidate("tech", cosine=0.90)
    # (base, diversity, candidate, components) — base == cosine for this fixture.
    scored = [
        (0.90, 1.0, tech, {"cosine": 0.90}),
        (0.50, 1.0, weak_sports, {"cosine": 0.50}),   # weaker sports appears FIRST
        (0.70, 1.0, strong_sports, {"cosine": 0.70}),
    ]

    result = scoring_loop._find_exploration_candidate(scored, state)

    assert result is not None
    base, _, cand, comps = result
    assert cand is strong_sports  # highest base in target category, not the first match
    assert comps["exploration_bonus"] == 0.15
    assert base == pytest.approx(0.70 + 0.15)
