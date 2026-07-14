"""moments: the sparse per-fixture schedule that replaced the poll grid. Key
properties: deterministic ids with no timestamp component (a rescheduled fixture
updates in place, never forks), at most pre+kickoff+result per fixture (vs up to 11
poll-grid docs before), and a settled fixture gets nothing.
"""

from __future__ import annotations

import dataclasses
from datetime import UTC, datetime, timedelta

from src.services.tracking import fields as f
from src.services.tracking.moments import (
    MAX_RESULT_CHECKS,
    PRE_LEAD_DEFAULT,
    PRE_LEAD_MAX,
    PRE_LEAD_MIN,
    PULSE_INTERVAL_INITIAL_S,
    PULSE_INTERVAL_MAX_S,
    PULSE_INTERVAL_MIN_S,
    RESULT_NOT_BEFORE_MARGIN,
    build_fetch_query,
    build_moments,
    build_pulse_checkpoint,
    clamped_pre_lead,
    clean_topic_descriptor,
    is_legacy_poll_phase,
    is_moment_phase,
    moment_id,
    next_pulse_interval,
    next_window_open,
    within_notify_window,
)
from src.services.tracking.models import Fixture

_NOW = datetime(2026, 7, 9, 12, 0, tzinfo=UTC)
_KICKOFF = datetime(2026, 7, 10, 18, 0, tzinfo=UTC)


def _fixture(**overrides) -> Fixture:
    base = Fixture(
        id="20260710-1800",
        topic_key="fifa-world-cup-2026",
        label="Spain vs Belgium",
        start_at=_KICKOFF,
        expected_end_at=_KICKOFF + timedelta(hours=2),
        kind=f.EVENT_KIND_SPAN,
    )
    return dataclasses.replace(base, **overrides) if overrides else base


def _by_phase(moments) -> dict:
    return {m.phase: m for m in moments}


def test_exactly_three_moments_for_an_upcoming_fixture():
    moments = build_moments(_fixture(), now=_NOW)
    phases = _by_phase(moments)
    assert set(phases) == {
        f.CHECKPOINT_PHASE_PRE, f.CHECKPOINT_PHASE_KICKOFF, f.CHECKPOINT_PHASE_RESULT,
    }
    assert phases[f.CHECKPOINT_PHASE_PRE].fire_at == _KICKOFF - PRE_LEAD_DEFAULT
    assert phases[f.CHECKPOINT_PHASE_KICKOFF].fire_at == _KICKOFF
    assert phases[f.CHECKPOINT_PHASE_RESULT].fire_at == _KICKOFF + timedelta(hours=2)
    for m in moments:
        assert m.fixture_id == "20260710-1800"
        assert m.status == f.CHECKPOINT_STATUS_PENDING


def test_moment_ids_are_deterministic_with_no_timestamp_component():
    # A moved kickoff must land on the SAME doc ids so the schedule updates in place.
    original = _by_phase(build_moments(_fixture(), now=_NOW))
    moved = _fixture(
        start_at=_KICKOFF + timedelta(hours=3),
        expected_end_at=_KICKOFF + timedelta(hours=5),
    )
    rescheduled = _by_phase(build_moments(moved, now=_NOW))
    for phase in original:
        assert original[phase].id == rescheduled[phase].id
        assert rescheduled[phase].fire_at == original[phase].fire_at + timedelta(hours=3)
    assert moment_id("t", "fx", "pre") == "t__fx__pre"


def test_past_pre_and_kickoff_are_not_enqueued_but_result_still_is():
    # Provisioned mid-match: no retroactive "kicks off soon", but the final result
    # still lands (checks shortly after now when the expected end already passed).
    mid_match_now = _KICKOFF + timedelta(minutes=50)
    phases = _by_phase(build_moments(_fixture(), now=mid_match_now))
    assert set(phases) == {f.CHECKPOINT_PHASE_RESULT}

    long_over_now = _KICKOFF + timedelta(days=1)
    phases = _by_phase(build_moments(_fixture(), now=long_over_now))
    assert phases[f.CHECKPOINT_PHASE_RESULT].fire_at == long_over_now + RESULT_NOT_BEFORE_MARGIN


def test_settled_fixtures_get_no_moments():
    assert build_moments(_fixture(status=f.FIXTURE_STATUS_FINISHED), now=_NOW) == []
    assert build_moments(_fixture(status=f.FIXTURE_STATUS_CANCELLED), now=_NOW) == []


def test_wake_override_carries_to_every_moment():
    moments = build_moments(_fixture(wake_override=True), now=_NOW)
    assert all(m.wake_override for m in moments)


def test_pre_lead_is_clamped():
    assert clamped_pre_lead(0) == PRE_LEAD_DEFAULT
    assert clamped_pre_lead(5) == PRE_LEAD_MIN
    assert clamped_pre_lead(45) == timedelta(minutes=45)
    assert clamped_pre_lead(600) == PRE_LEAD_MAX


def test_legacy_poll_phase_discriminator():
    # Old poll-grid docs: legacy phase values, or a non-pulse doc with no fixture
    # binding. Both must read as legacy so the fire path expires them on sight.
    assert is_legacy_poll_phase(f.CHECKPOINT_PHASE_LIVE, "") is True
    assert is_legacy_poll_phase(f.CHECKPOINT_PHASE_POST, "") is True
    assert is_legacy_poll_phase(f.CHECKPOINT_PHASE_MILESTONE, "") is True
    assert is_legacy_poll_phase(f.CHECKPOINT_PHASE_PRE, "") is True  # old pre: no fixture_id
    assert is_legacy_poll_phase(f.CHECKPOINT_PHASE_PRE, "20260710-1800") is False
    assert is_legacy_poll_phase(f.CHECKPOINT_PHASE_PULSE, "") is False  # the pulse survives

    assert is_moment_phase(f.CHECKPOINT_PHASE_RESULT) is True
    assert is_moment_phase(f.CHECKPOINT_PHASE_LIVE) is False


def test_result_recheck_budget_is_bounded():
    # The only polling left in the engine: a narrow, capped uncertainty window.
    assert MAX_RESULT_CHECKS == 5


# ── adaptive pulse (relocated from the retired test_tracking_schedule.py) ─────
def test_next_pulse_interval_tightens_when_new_loosens_when_not():
    base = PULSE_INTERVAL_INITIAL_S
    # Found something new -> poll sooner (halve); nothing new -> back off (x1.5).
    assert next_pulse_interval(base, found_new=True) == int(base * 0.5)
    assert next_pulse_interval(base, found_new=False) == int(base * 1.5)


def test_next_pulse_interval_clamps_to_min_and_max():
    assert next_pulse_interval(PULSE_INTERVAL_MIN_S, found_new=True) == PULSE_INTERVAL_MIN_S
    assert next_pulse_interval(PULSE_INTERVAL_MAX_S, found_new=False) == PULSE_INTERVAL_MAX_S


def test_next_pulse_interval_zero_starts_from_initial():
    # A topic written before the field existed (0) gets a sane first cadence, not 0.
    assert next_pulse_interval(0, found_new=True) == int(PULSE_INTERVAL_INITIAL_S * 0.5)


def test_build_pulse_checkpoint_shape():
    fire_at = datetime(2026, 6, 15, 6, tzinfo=UTC)
    cp = build_pulse_checkpoint("gta-6", fire_at=fire_at, now=datetime(2026, 6, 15, tzinfo=UTC))
    assert cp.id == "gta-6__pulse"
    assert cp.phase == f.CHECKPOINT_PHASE_PULSE
    assert cp.fire_at == fire_at
    assert cp.status == f.CHECKPOINT_STATUS_PENDING


# ── notify window / local quiet hours (relocated) ─────────────────────────────
def test_within_notify_window_basic_and_24h():
    noon = datetime(2026, 6, 19, 12, tzinfo=UTC)
    night = datetime(2026, 6, 19, 3, tzinfo=UTC)
    assert within_notify_window(noon, tz_name="UTC", start_hour=8, end_hour=23) is True
    assert within_notify_window(night, tz_name="UTC", start_hour=8, end_hour=23) is False
    # start == end -> 24h window, always on.
    assert within_notify_window(night, tz_name="UTC", start_hour=0, end_hour=0) is True


def test_within_notify_window_wraps_past_midnight():
    # A 22:00->06:00 window (night owl). 23:00 is in, 12:00 is out.
    assert within_notify_window(datetime(2026, 6, 19, 23, tzinfo=UTC), tz_name="UTC", start_hour=22, end_hour=6) is True
    assert within_notify_window(datetime(2026, 6, 19, 12, tzinfo=UTC), tz_name="UTC", start_hour=22, end_hour=6) is False


def test_within_notify_window_respects_timezone():
    # 12:00 UTC is 08:00 in New York (EDT, summer) -> just inside an 8..23 window there.
    twelve_utc = datetime(2026, 6, 19, 12, tzinfo=UTC)
    eleven_utc = datetime(2026, 6, 19, 11, tzinfo=UTC)  # 07:00 EDT, before the window
    assert within_notify_window(twelve_utc, tz_name="America/New_York", start_hour=8, end_hour=23) is True
    assert within_notify_window(eleven_utc, tz_name="America/New_York", start_hour=8, end_hour=23) is False


def test_within_notify_window_bad_timezone_is_fail_open_utc():
    # A garbage tz must not raise; it falls back to UTC (never silently suppress).
    noon = datetime(2026, 6, 19, 12, tzinfo=UTC)
    assert within_notify_window(noon, tz_name="Not/AZone", start_hour=8, end_hour=23) is True


def test_next_window_open_returns_now_when_inside():
    noon = datetime(2026, 6, 19, 12, tzinfo=UTC)
    assert next_window_open(noon, tz_name="UTC", start_hour=8, end_hour=23) == noon


def test_next_window_open_jumps_to_next_start():
    # 03:00 UTC, window 8..23 -> opens at 08:00 the same day.
    pre_dawn = datetime(2026, 6, 19, 3, tzinfo=UTC)
    assert next_window_open(pre_dawn, tz_name="UTC", start_hour=8, end_hour=23) == datetime(2026, 6, 19, 8, tzinfo=UTC)
    # 23:30 UTC (after end) -> opens at 08:00 the NEXT day.
    late = datetime(2026, 6, 19, 23, 30, tzinfo=UTC)
    assert next_window_open(late, tz_name="UTC", start_hour=8, end_hour=23) == datetime(2026, 6, 20, 8, tzinfo=UTC)


# ── fetch query construction (relocated; 2026-06-19 "composer abstained" fix) ─
def test_clean_topic_descriptor_strips_trailing_request_clause():
    # The exact stored query that made every WC checkpoint fetch a generic jumble.
    raw = "FIFA World Cup 2026 - keep me posted on all results, scores, and key updates until the tournament ends"
    assert clean_topic_descriptor(raw) == "FIFA World Cup 2026"


def test_clean_topic_descriptor_strips_leading_request_verb():
    assert clean_topic_descriptor("let me know when GRRM releases Winds of Winter") == \
        "GRRM releases Winds of Winter"
    assert clean_topic_descriptor("keep me updated on the 2026 US midterms") == "2026 US midterms"
    assert clean_topic_descriptor("notify me about the next Fed interest rate decision") == \
        "next Fed interest rate decision"


def test_clean_topic_descriptor_leaves_a_clean_subject_untouched():
    # A query that is already just the subject is returned unchanged (idempotent).
    assert clean_topic_descriptor("Tesla stock and product launches") == "Tesla stock and product launches"
    assert clean_topic_descriptor("major earthquake near Tokyo") == "major earthquake near Tokyo"


def test_clean_topic_descriptor_never_empties():
    # If the heuristics would consume everything, the original survives (never a blank query).
    assert clean_topic_descriptor("keep me posted") == "keep me posted"
    assert clean_topic_descriptor("   ") == ""


def test_build_fetch_query_fixture_moment_anchors_specific_beat():
    # A fixture moment searches its OWN match, anchored by the clean topic, instead of
    # the verbose topic sentence every checkpoint used to share.
    q = build_fetch_query(
        event_label="United States vs. Australia",
        research_query="FIFA World Cup 2026 - keep me posted on all results until the tournament ends",
        title="FIFA World Cup 2026",
    )
    assert q == "United States vs. Australia FIFA World Cup 2026"


def test_build_fetch_query_pulse_uses_clean_topic_query():
    # A pulse / no-label checkpoint has no specific beat -> the cleaned topic query.
    q = build_fetch_query(
        event_label="",
        research_query="let me know when GRRM releases Winds of Winter",
        title="Winds of Winter",
    )
    assert q == "GRRM releases Winds of Winter"


def test_build_fetch_query_falls_back_to_title_when_no_query():
    q = build_fetch_query(event_label="", research_query="", title="Tesla news")
    assert q == "Tesla news"
