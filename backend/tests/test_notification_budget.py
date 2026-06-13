"""Unified notification budget — the cross-decider daily ceiling + spacing.

The cap / spacing / daily-reset policy is a pure function so it is pinned here
without mocking Firestore transactions. The flag-off no-op and fail-open
behaviours are also covered, because those are the safety guarantees that let
this land on every live notification path without changing current behaviour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from src.services import notification_budget as nb
from src.services.notification_budget import (
    FIELD_LAST_NOTIFICATION_AT,
    FIELD_PROACTIVE_SENDS_TODAY,
    FIELD_SENDS_TODAY_DATE,
    GLOBAL_DAILY_PROACTIVE_CAP,
    MIN_PROACTIVE_SPACING,
    BudgetDecision,
    evaluate_proactive_claim,
)

NOW = datetime(2026, 6, 10, 18, 0, tzinfo=UTC)
TODAY = "2026-06-10"


def test_empty_doc_allows():
    assert evaluate_proactive_claim({}, local_date=TODAY, now=NOW).allowed


def test_under_cap_allows():
    data = {FIELD_SENDS_TODAY_DATE: TODAY, FIELD_PROACTIVE_SENDS_TODAY: GLOBAL_DAILY_PROACTIVE_CAP - 1}
    # No last_notification_at -> no spacing block.
    assert evaluate_proactive_claim(data, local_date=TODAY, now=NOW).allowed


def test_at_cap_blocks():
    data = {FIELD_SENDS_TODAY_DATE: TODAY, FIELD_PROACTIVE_SENDS_TODAY: GLOBAL_DAILY_PROACTIVE_CAP}
    decision = evaluate_proactive_claim(data, local_date=TODAY, now=NOW)
    assert not decision.allowed and decision.reason == "global_daily_cap"


def test_count_from_a_previous_day_does_not_count():
    # Yesterday's exhausted count must not block today.
    data = {FIELD_SENDS_TODAY_DATE: "2026-06-09", FIELD_PROACTIVE_SENDS_TODAY: 99}
    assert evaluate_proactive_claim(data, local_date=TODAY, now=NOW).allowed


def test_spacing_blocks_within_window():
    data = {
        FIELD_SENDS_TODAY_DATE: TODAY,
        FIELD_PROACTIVE_SENDS_TODAY: 1,
        FIELD_LAST_NOTIFICATION_AT: NOW - (MIN_PROACTIVE_SPACING - timedelta(minutes=1)),
    }
    decision = evaluate_proactive_claim(data, local_date=TODAY, now=NOW)
    assert not decision.allowed and decision.reason == "global_spacing"


def test_spacing_allows_past_window():
    data = {
        FIELD_SENDS_TODAY_DATE: TODAY,
        FIELD_PROACTIVE_SENDS_TODAY: 1,
        FIELD_LAST_NOTIFICATION_AT: NOW - (MIN_PROACTIVE_SPACING + timedelta(minutes=1)),
    }
    assert evaluate_proactive_claim(data, local_date=TODAY, now=NOW).allowed


def test_naive_last_timestamp_is_treated_as_utc():
    naive = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
    data = {FIELD_SENDS_TODAY_DATE: TODAY, FIELD_LAST_NOTIFICATION_AT: naive}
    decision = evaluate_proactive_claim(data, local_date=TODAY, now=NOW)
    assert not decision.allowed and decision.reason == "global_spacing"


# ── Flag-off and fail-open safety ────────────────────────────────────────────

async def test_claim_is_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(nb.settings, "UNIFIED_NOTIFICATION_BUDGET_ENABLED", False)
    # Must not touch Firestore at all when disabled.
    with patch.object(nb, "admin_firestore", side_effect=AssertionError("must not be called")):
        result = await nb.try_claim_proactive_slot("u1", source="signal_engine", user_local_date=TODAY)
    assert result.allowed


async def test_committed_is_noop_when_flag_off(monkeypatch):
    monkeypatch.setattr(nb.settings, "UNIFIED_NOTIFICATION_BUDGET_ENABLED", False)
    with patch.object(nb, "admin_firestore", side_effect=AssertionError("must not be called")):
        await nb.record_committed_send("u1", source="reminder")  # no raise = pass


async def test_claim_fails_open_on_error(monkeypatch):
    monkeypatch.setattr(nb.settings, "UNIFIED_NOTIFICATION_BUDGET_ENABLED", True)
    # A Firestore explosion must never silence notifications.
    with patch.object(nb, "admin_firestore", side_effect=RuntimeError("firestore down")):
        result = await nb.try_claim_proactive_slot("u1", source="signal_engine", user_local_date=TODAY)
    assert result.allowed
    assert isinstance(result, BudgetDecision)
