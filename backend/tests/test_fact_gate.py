"""fact_gate: the transition table and dedup-key stability that replaced text-hash
dedup. The acid property: keys derive from (topic, fixture, destination state) and
NEVER from composed wording, so a reworded composition of the same fact cannot
produce a fresh key — the exact failure that let one fact send six times on
2026-07-10.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.services.tracking import fields as f
from src.services.tracking.fact_gate import (
    CONTENT_WINDOW_LEAD,
    FactState,
    coerce_fact_status,
    content_within_window,
    development_dedup_key,
    extract_transition,
    is_result_send_worthy,
    moment_dedup_key,
    result_dedup_key,
    slug_development_key,
    transition_destination,
)

_START = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)


# ── transition table ─────────────────────────────────────────────────────────
def test_forward_transitions_are_detected():
    scheduled = FactState(status=f.FIXTURE_STATUS_SCHEDULED)
    live = FactState(status=f.FIXTURE_STATUS_LIVE)
    finished = FactState(status=f.FIXTURE_STATUS_FINISHED, score="1-0", winner="Spain")

    assert extract_transition(scheduled, live) == "scheduled->live"
    assert extract_transition(scheduled, finished) == "scheduled->finished"
    assert extract_transition(live, finished) == "live->finished"


def test_same_state_is_never_a_transition_regardless_of_details():
    # The core anti-repeat property: once finished, a re-fetch that words the same
    # outcome differently (different score string, added color) is NOT a transition.
    prior = FactState(status=f.FIXTURE_STATUS_FINISHED, score="1-0", winner="Spain")
    seen = FactState(status=f.FIXTURE_STATUS_FINISHED, score="1 - 0", winner="Spain", note="late goal")
    assert extract_transition(prior, seen) is None


def test_backward_movement_is_rejected():
    # A stale article describing the match as upcoming AFTER it finished (the
    # "kicks off soon, 40 minutes after kickoff" bug) must never transition.
    finished = FactState(status=f.FIXTURE_STATUS_FINISHED, winner="Spain")
    scheduled = FactState(status=f.FIXTURE_STATUS_SCHEDULED)
    live = FactState(status=f.FIXTURE_STATUS_LIVE)
    assert extract_transition(finished, scheduled) is None
    assert extract_transition(finished, live) is None
    assert extract_transition(live, scheduled) is None


def test_cancellation_transitions_from_any_live_state_and_is_terminal():
    scheduled = FactState(status=f.FIXTURE_STATUS_SCHEDULED)
    live = FactState(status=f.FIXTURE_STATUS_LIVE)
    cancelled = FactState(status=f.FIXTURE_STATUS_CANCELLED, note="postponed to Saturday")
    assert extract_transition(scheduled, cancelled) == "scheduled->cancelled"
    assert extract_transition(live, cancelled) == "live->cancelled"
    # Terminal: nothing moves a cancelled fixture (not even "finished").
    finished = FactState(status=f.FIXTURE_STATUS_FINISHED)
    assert extract_transition(cancelled, finished) is None


def test_result_send_worthiness():
    assert is_result_send_worthy("scheduled->finished") is True
    assert is_result_send_worthy("live->finished") is True
    assert is_result_send_worthy("scheduled->cancelled") is True
    # "It started" belongs to the kickoff moment, not a result push.
    assert is_result_send_worthy("scheduled->live") is False
    assert is_result_send_worthy(None) is False


def test_coerce_fact_status_closes_the_state_set():
    assert coerce_fact_status("FINISHED") == f.FIXTURE_STATUS_FINISHED
    assert coerce_fact_status(" live ") == f.FIXTURE_STATUS_LIVE
    assert coerce_fact_status("full-time") == f.FIXTURE_STATUS_SCHEDULED  # off-list coerces
    assert coerce_fact_status("") == f.FIXTURE_STATUS_SCHEDULED


# ── dedup keys ───────────────────────────────────────────────────────────────
def test_result_dedup_key_is_wording_independent_and_edge_independent():
    # "scheduled->finished" and "live->finished" describe the same send-worthy fact
    # (the fixture finished); both map to ONE key so the second can never send.
    key_a = result_dedup_key("fifa-world-cup-2026", "20260710-1800", "scheduled->finished")
    key_b = result_dedup_key("fifa-world-cup-2026", "20260710-1800", "live->finished")
    assert key_a == key_b == "tracker_fifa-world-cup-2026_20260710-1800_finished"


def test_dedup_keys_differ_across_fixtures_and_moments():
    finished_a = result_dedup_key("t", "20260710-1800", "live->finished")
    finished_b = result_dedup_key("t", "20260711-1600", "live->finished")
    assert finished_a != finished_b

    pre = moment_dedup_key("t", "20260710-1800", f.CHECKPOINT_PHASE_PRE)
    kickoff = moment_dedup_key("t", "20260710-1800", f.CHECKPOINT_PHASE_KICKOFF)
    assert len({finished_a, pre, kickoff}) == 3


def test_transition_destination():
    assert transition_destination("scheduled->finished") == "finished"
    assert transition_destination("") == ""


def test_development_key_slug_collides_trivial_rewordings():
    a = slug_development_key("Spain advances to the semi-finals!")
    b = slug_development_key("spain ADVANCES to the semi finals")
    assert a == b
    assert development_dedup_key("t", a) == development_dedup_key("t", b)
    assert slug_development_key("   ") == ""


# ── temporal content window ──────────────────────────────────────────────────
def test_content_window_rejects_pre_window_articles():
    # An article published two days before kickoff cannot carry the fixture's outcome
    # — the "date set for Thursday July 9" pushed on July 10 came from exactly this.
    stale = _START - timedelta(days=2)
    fresh = _START + timedelta(hours=1)
    assert content_within_window(stale, fixture_start_at=_START) is False
    assert content_within_window(fresh, fixture_start_at=_START) is True
    # Just inside the lead margin (preview minutes before kickoff) passes.
    assert content_within_window(_START - CONTENT_WINDOW_LEAD, fixture_start_at=_START) is True


def test_content_window_passes_undated_content():
    # brave/grounded carry no publish dates; the extraction LLM's
    # refers_to_this_fixture judgment is the gate there instead.
    assert content_within_window(None, fixture_start_at=_START) is True
