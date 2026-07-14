"""
Coverage for the one-vocabulary relevance gate (Layer 2/3) + soft region preference.

The gate is the change that makes notifications relevant without muting anyone. The
two failure modes it must never have are pinned here:
  1. It actually filters out-of-interest content (Gate A works), AND
  2. it NEVER silently zeroes a user whose interests no source can satisfy
     (the existing-beta-user no-blackout safeguard — the exact "zero rows looks
     healthy" outage this project has been bitten by).
Plus: legacy pool-vocab affinity keys still contribute through the map, region is a
soft nudge (never a hard filter), and Gate B (LLM relevance) can stop an off item.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.signal_engine import feature_store, scoring_loop
from src.services.signal_engine.content_pool import ScoredCandidate


def _candidate(category: str, cosine: float, region: str = "") -> ScoredCandidate:
    return ScoredCandidate(
        content_id=f"c_{category}_{int(cosine * 100)}_{region}",
        source="google_news",
        category=category,
        title="A headline",
        body="Some body text.",
        url="https://example.com/a",
        embedding=[0.1, 0.2, 0.3],
        freshness_ts=datetime.now(UTC),
        cosine_similarity=cosine,
        sub_category="",
        region=region,
    )


def _ready_state() -> feature_store.SignalStoreState:
    state = feature_store.SignalStoreState()
    state.bootstrap_done = True
    state.recent_sends_backfilled = True
    state.user_vector = [0.1] * feature_store.USER_VECTOR_DIMENSION
    state.sends_today = 0
    state.consecutive_no_open_ticks = 0
    state.time_slot_open_rates = [1.0] * feature_store.TIME_SLOTS_PER_DAY
    return state


@pytest.fixture
def patched(monkeypatch):
    """Stub the I/O around the real gate + scoring math. Per-test we set the user
    doc (declared interests / locale), the candidate list, and the framer result."""
    monkeypatch.setattr(scoring_loop, "_read_user_aura", AsyncMock(return_value={}))
    monkeypatch.setattr(scoring_loop, "_sweep_timeouts", AsyncMock(return_value=0))
    monkeypatch.setattr(scoring_loop, "_should_refresh_user_vector", lambda state: False)
    monkeypatch.setattr(scoring_loop, "is_within_active_hours", lambda *a, **k: True)
    monkeypatch.setattr(
        scoring_loop, "_build_framing_context", lambda *a, **k: scoring_loop.UserFramingContext()
    )
    monkeypatch.setattr(
        scoring_loop,
        "frame_notification",
        AsyncMock(return_value=SimpleNamespace(
            title="t", body="b", opening_chat_message="hey",
            is_relevant=True, relevance_reason="matches your interest",
            content_kind="read",
        )),
    )
    # Post-cutover, the scoring tick ENQUEUES via orchestrator.submit (the real send +
    # outcome + funnel happen later in the drain). notifications_sent counts the enqueue,
    # so the gate assertions are unchanged; we just patch the new seam.
    submit_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(scoring_loop.orchestrator, "submit", submit_mock)
    monkeypatch.setattr(feature_store, "write_outcome_pending", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop, "_safe_write_state", AsyncMock(return_value=None))
    monkeypatch.setattr(scoring_loop.posthog_client, "capture_event", AsyncMock())
    return submit_mock


async def _run(monkeypatch, *, user_doc, candidates):
    monkeypatch.setattr(scoring_loop, "_load_user_doc", AsyncMock(return_value=user_doc))
    monkeypatch.setattr(
        scoring_loop, "find_nearest_for_user", AsyncMock(return_value=candidates)
    )
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=_ready_state())):
        await scoring_loop._score_one_user("uid", MagicMock(), summary, [])
    return summary


# --- Gate A -----------------------------------------------------------------

async def test_gate_a_filters_out_of_allow_set(patched, monkeypatch):
    """Declared interest = technology only; a strong sports candidate must NOT send."""
    summary = await _run(
        monkeypatch,
        user_doc={"timezone": "UTC", "onboarding_interests": ["technology_computing"]},
        candidates=[_candidate("sports", cosine=0.9)],  # off-interest, but strong
    )
    assert summary.notifications_sent == 0
    patched.assert_not_awaited()


async def test_gate_a_passes_in_allow_set(patched, monkeypatch):
    """The same user DOES get a strong in-interest (tech) candidate."""
    summary = await _run(
        monkeypatch,
        user_doc={"timezone": "UTC", "onboarding_interests": ["technology_computing"]},
        candidates=[_candidate("tech", cosine=0.9)],  # 'tech' normalises to technology_computing
    )
    assert summary.notifications_sent == 1
    patched.assert_awaited_once()


async def test_no_blackout_when_no_producible_interest(patched, monkeypatch):
    """Existing-user safeguard: a user whose ONLY interest is non-producible
    (automotive) must still receive a relevant producible candidate — Gate A is
    skipped, not silently zeroing them."""
    warn = MagicMock()
    monkeypatch.setattr(scoring_loop.logger, "warn", warn)
    summary = await _run(
        monkeypatch,
        user_doc={"timezone": "UTC", "onboarding_interests": ["automotive"]},
        candidates=[_candidate("sports", cosine=0.9)],
    )
    assert summary.notifications_sent == 1  # NOT muted
    # Fail loud: the no-producible-category condition logs a warning.
    assert any("no producible category" in str(c) for c in warn.call_args_list)


async def test_legacy_affinity_key_contributes_via_map(patched, monkeypatch):
    """A legacy pool-vocab affinity key ('tech': 0.9) must grant the taxonomy slug
    technology_computing into the allow-list so a tech candidate sends."""
    state = _ready_state()
    state.category_affinities = {"tech": 0.9}  # old vocab, above the 0.5 bar
    monkeypatch.setattr(scoring_loop, "_load_user_doc", AsyncMock(return_value={"timezone": "UTC"}))
    monkeypatch.setattr(
        scoring_loop, "find_nearest_for_user",
        AsyncMock(return_value=[_candidate("tech", cosine=0.9)]),
    )
    summary = scoring_loop.TickSummary()
    with patch.object(feature_store, "read_state", AsyncMock(return_value=state)):
        await scoring_loop._score_one_user("uid", MagicMock(), summary, [])
    assert summary.notifications_sent == 1


# --- Gate B (LLM relevance confirm) -----------------------------------------

async def test_gate_b_not_relevant_blocks_send(patched, monkeypatch):
    """Even an in-allow-set candidate is dropped if the framer says not relevant."""
    monkeypatch.setattr(
        scoring_loop,
        "frame_notification",
        AsyncMock(return_value=SimpleNamespace(
            title="t", body="b", opening_chat_message="hey",
            is_relevant=False, relevance_reason="off topic", content_kind="read",
        )),
    )
    summary = await _run(
        monkeypatch,
        user_doc={"timezone": "UTC", "onboarding_interests": ["technology_computing"]},
        candidates=[_candidate("tech", cosine=0.9)],
    )
    assert summary.notifications_sent == 0
    patched.assert_not_awaited()


# --- Soft region preference --------------------------------------------------

def test_region_multiplier_is_a_soft_nudge():
    # Match boosts, mismatch softens, unknown either side stays neutral.
    assert scoring_loop._region_multiplier("IN", "IN") == scoring_loop.REGION_MATCH_BOOST
    assert scoring_loop._region_multiplier("IN", "US") == scoring_loop.REGION_MISMATCH_PENALTY
    assert scoring_loop._region_multiplier("", "US") == 1.0
    assert scoring_loop._region_multiplier("IN", "") == 1.0


def test_region_from_locale_extracts_country():
    assert scoring_loop._region_from_locale("en-IN") == "IN"
    assert scoring_loop._region_from_locale("en_US") == "US"
    assert scoring_loop._region_from_locale("GB") == "GB"
    assert scoring_loop._region_from_locale("") == ""


async def test_region_mismatch_never_hard_filters(patched, monkeypatch):
    """A foreign-region candidate is softened, never blocked: a US user still gets
    an IN-region story when it's the only relevant one and clears the bar."""
    summary = await _run(
        monkeypatch,
        user_doc={"timezone": "UTC", "locale": "en-US",
                  "onboarding_interests": ["news_current_affairs"]},
        candidates=[_candidate("news", cosine=0.9, region="IN")],
    )
    assert summary.notifications_sent == 1


# --- allow-set builder (pure) -----------------------------------------------

def test_build_allow_set_unions_and_intersects():
    now = datetime(2026, 6, 11, tzinfo=UTC)
    aura = {
        "interests": {
            "technology_computing": {"weight": 3.0, "last_seen": now.isoformat()},
            "automotive": {"weight": 2.0, "last_seen": now.isoformat()},
        }
    }
    user_doc = {"onboarding_interests": ["sports"]}
    state = feature_store.SignalStoreState()
    state.category_affinities = {"news": 0.9, "health_medical": 0.2}  # only news clears 0.5

    allow, effective = scoring_loop._build_category_allow_set(aura, user_doc, state, now)

    assert {"technology_computing", "automotive", "sports", "news_current_affairs"} <= allow
    # automotive is in allow but not producible, so it drops out of effective.
    assert "automotive" in allow and "automotive" not in effective
    assert {"technology_computing", "sports", "news_current_affairs"} <= effective
