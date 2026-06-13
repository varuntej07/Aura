"""Tests for the pure icebreaker scheduling logic (the dice roll + target hour).

Determinism is the safety property: two overlapping scheduler ticks must compute
the IDENTICAL schedule, so they can never disagree on which days / hour fire.
"""

from __future__ import annotations

from datetime import datetime

from src.services.icebreaker.scheduler_logic import (
    ICEBREAKER_DAYS_PER_WEEK,
    TARGET_HOUR_EARLIEST,
    TARGET_HOUR_LATEST,
    current_week_start_date,
    is_scheduled_today,
    roll_week_dates,
    target_local_hour,
)


def test_week_start_is_the_sunday_on_or_before_today():
    # 2026-06-12 is a Friday; the Sunday of its week is 2026-06-07.
    friday = datetime.fromisoformat("2026-06-12T10:00:00")
    assert current_week_start_date(friday) == "2026-06-07"
    # A Sunday maps to itself.
    sunday = datetime.fromisoformat("2026-06-07T23:00:00")
    assert current_week_start_date(sunday) == "2026-06-07"


def test_roll_is_deterministic_for_same_user_and_week():
    a = roll_week_dates("user-123", "2026-06-07")
    b = roll_week_dates("user-123", "2026-06-07")
    assert a == b  # two ticks never disagree


def test_roll_picks_distinct_in_week_count():
    dates = roll_week_dates("user-123", "2026-06-07")
    assert len(dates) == ICEBREAKER_DAYS_PER_WEEK
    assert len(set(dates)) == ICEBREAKER_DAYS_PER_WEEK
    # All within the Sun..Sat window of that week.
    assert all("2026-06-07" <= d <= "2026-06-13" for d in dates)
    assert dates == sorted(dates)


def test_roll_differs_across_users_and_weeks():
    u1 = roll_week_dates("user-aaa", "2026-06-07")
    u2 = roll_week_dates("user-bbb", "2026-06-07")
    next_week = roll_week_dates("user-aaa", "2026-06-14")
    # Not a hard guarantee, but with different seeds these should not all collide.
    assert not (u1 == u2 == next_week)


def test_target_hour_is_deterministic_and_in_window():
    h1 = target_local_hour("user-123", "2026-06-10")
    h2 = target_local_hour("user-123", "2026-06-10")
    assert h1 == h2
    assert TARGET_HOUR_EARLIEST <= h1 <= TARGET_HOUR_LATEST


def test_is_scheduled_today():
    assert is_scheduled_today("2026-06-10", ["2026-06-08", "2026-06-10"])
    assert not is_scheduled_today("2026-06-09", ["2026-06-08", "2026-06-10"])
    assert not is_scheduled_today("2026-06-10", [])
