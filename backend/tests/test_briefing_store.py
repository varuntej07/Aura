"""Tests for the daily-briefing claim logic — the idempotency guarantee.

``evaluate_claim`` is the pure core of the Firestore transaction. The critical test
simulates two overlapping ticks: the first claims today's slot, then the SAME doc
(with the first claim's write applied) is fed to a second call, which must return
``in_progress`` — exactly what a concurrent loser tick sees, so at most one briefing
is ever generated per user per local date. A failed prior attempt may be re-claimed
(retry), and a stale ``generating`` claim (the claimer crashed) may be re-claimed so
a hard crash never burns the day permanently.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from src.services.briefing import fields as f
from src.services.briefing.briefing_store import (
    STALE_CLAIM_MINUTES,
    evaluate_claim,
    lookback_dates,
)

_TODAY = "2026-06-13"
_NOW = datetime(2026, 6, 13, 6, 5, tzinfo=UTC)


def _apply(data: dict, update: dict) -> dict:
    """Simulate the Firestore merge=True write of `update` onto `data`."""
    return {**data, **update}


def test_fresh_doc_claims_today():
    update, result = evaluate_claim({}, local_date=_TODAY, now=_NOW)
    assert result.claimed is True
    assert result.reason == "claimed"
    assert update[f.FIELD_STATUS] == f.STATUS_GENERATING
    assert update[f.FIELD_LOCAL_DATE] == _TODAY
    assert update[f.FIELD_CREATED_AT] == _NOW


def test_second_overlapping_tick_stands_down():
    update1, result1 = evaluate_claim({}, local_date=_TODAY, now=_NOW)
    assert result1.claimed is True

    # Second tick sees the first tick's write — must NOT claim again.
    data_after = _apply({}, update1)
    update2, result2 = evaluate_claim(data_after, local_date=_TODAY, now=_NOW)
    assert result2.claimed is False
    assert result2.reason == "in_progress"
    assert update2 == {}


def test_ready_doc_is_already_generated():
    data = {f.FIELD_STATUS: f.STATUS_READY, f.FIELD_LOCAL_DATE: _TODAY}
    update, result = evaluate_claim(data, local_date=_TODAY, now=_NOW)
    assert result.claimed is False
    assert result.reason == "already_generated"
    assert update == {}


def test_skipped_doc_is_already_generated():
    data = {f.FIELD_STATUS: f.STATUS_SKIPPED, f.FIELD_LOCAL_DATE: _TODAY}
    _update, result = evaluate_claim(data, local_date=_TODAY, now=_NOW)
    assert result.claimed is False
    assert result.reason == "already_generated"


def test_failed_doc_is_reclaimed_for_retry():
    data = {f.FIELD_STATUS: f.STATUS_FAILED, f.FIELD_LOCAL_DATE: _TODAY}
    update, result = evaluate_claim(data, local_date=_TODAY, now=_NOW)
    assert result.claimed is True
    assert update[f.FIELD_STATUS] == f.STATUS_GENERATING


def test_fresh_generating_claim_blocks():
    """A `generating` claim made moments ago must block a second tick."""
    data = {
        f.FIELD_STATUS: f.STATUS_GENERATING,
        f.FIELD_LOCAL_DATE: _TODAY,
        f.FIELD_CREATED_AT: _NOW - timedelta(minutes=1),
    }
    _update, result = evaluate_claim(data, local_date=_TODAY, now=_NOW)
    assert result.claimed is False
    assert result.reason == "in_progress"


def test_stale_generating_claim_is_reclaimed():
    """A `generating` claim older than STALE_CLAIM_MINUTES (claimer crashed) may be
    re-claimed so a hard crash never permanently burns the day."""
    data = {
        f.FIELD_STATUS: f.STATUS_GENERATING,
        f.FIELD_LOCAL_DATE: _TODAY,
        f.FIELD_CREATED_AT: _NOW - timedelta(minutes=STALE_CLAIM_MINUTES + 1),
    }
    update, result = evaluate_claim(data, local_date=_TODAY, now=_NOW)
    assert result.claimed is True
    assert update[f.FIELD_STATUS] == f.STATUS_GENERATING


def test_lookback_dates_newest_first():
    assert lookback_dates("2026-06-16", 3) == ["2026-06-16", "2026-06-15", "2026-06-14"]


def test_lookback_dates_crosses_month_boundary():
    assert lookback_dates("2026-06-01", 2) == ["2026-06-01", "2026-05-31"]


def test_lookback_dates_bad_input_returns_self():
    assert lookback_dates("not-a-date", 5) == ["not-a-date"]
