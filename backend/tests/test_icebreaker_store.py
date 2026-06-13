"""Tests for the icebreaker claim logic — the idempotency guarantee.

`evaluate_claim` is the pure core of the Firestore transaction. The critical test
simulates two overlapping ticks: the first claims today, then the SAME doc (with
the first claim's write applied) is fed to a second call, which must return
`already_sent_today` — exactly what a concurrent loser tick sees, so at most one
icebreaker is ever sent per user per day.
"""

from __future__ import annotations

from src.services.icebreaker import fields as f
from src.services.icebreaker.icebreaker_store import evaluate_claim

_WEEK = "2026-06-07"
_ROLLED = ["2026-06-08", "2026-06-10", "2026-06-12"]
_TODAY = "2026-06-10"


def _apply(data: dict, update: dict) -> dict:
    """Simulate the Firestore merge=True write of `update` onto `data`."""
    return {**data, **update}


def test_fresh_doc_rolls_week_and_claims_today():
    update, result = evaluate_claim(
        {}, local_date=_TODAY, week_start_date=_WEEK, rolled_dates=_ROLLED
    )
    assert result.claimed is True
    assert result.reason == "claimed"
    assert update[f.FIELD_WEEK_START_DATE] == _WEEK
    assert update[f.FIELD_SCHEDULED_DATES] == _ROLLED
    assert update[f.FIELD_LAST_SENT_DATE] == _TODAY


def test_second_overlapping_tick_stands_down():
    # First tick claims.
    update1, result1 = evaluate_claim(
        {}, local_date=_TODAY, week_start_date=_WEEK, rolled_dates=_ROLLED
    )
    assert result1.claimed is True

    # Second tick sees the first tick's write — must NOT claim again.
    data_after = _apply({}, update1)
    _update2, result2 = evaluate_claim(
        data_after, local_date=_TODAY, week_start_date=_WEEK, rolled_dates=_ROLLED
    )
    assert result2.claimed is False
    assert result2.reason == "already_sent_today"


def test_not_a_scheduled_day_does_not_claim():
    _update, result = evaluate_claim(
        {}, local_date="2026-06-09", week_start_date=_WEEK, rolled_dates=_ROLLED
    )
    assert result.claimed is False
    assert result.reason == "not_scheduled_today"


def test_stale_week_is_rerolled():
    old = {
        f.FIELD_WEEK_START_DATE: "2026-05-31",
        f.FIELD_SCHEDULED_DATES: ["2026-06-01"],
        f.FIELD_LAST_SENT_DATE: "2026-06-01",
    }
    update, result = evaluate_claim(
        old, local_date=_TODAY, week_start_date=_WEEK, rolled_dates=_ROLLED
    )
    assert update[f.FIELD_SCHEDULED_DATES] == _ROLLED
    assert result.claimed is True


def test_recent_topics_passed_through_for_planner():
    data = {f.FIELD_RECENT_OPENER_TOPICS: ["weekend plans", "Diwali"]}
    _update, result = evaluate_claim(
        data, local_date=_TODAY, week_start_date=_WEEK, rolled_dates=_ROLLED
    )
    assert result.recent_opener_topics == ["weekend plans", "Diwali"]
