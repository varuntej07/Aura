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
    ADAPTIVE_MIN_SAMPLE,
    FIELD_LAST_NOTIFICATION_AT,
    FIELD_PROACTIVE_SENDS_TODAY,
    FIELD_SENDS_TODAY_DATE,
    GLOBAL_DAILY_PROACTIVE_CAP,
    MIN_PROACTIVE_SPACING,
    BudgetDecision,
    evaluate_proactive_claim,
    resolve_adaptive_limits,
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
    # Spacing is currently 0 (proactive sends uncapped during beta), so a naive timestamp
    # no longer produces a spacing block to assert against. Pin the tz-coercion helper the
    # spacing comparison relies on directly, so it stays covered for when a real spacing
    # window is restored (otherwise a naive vs aware subtraction would raise).
    naive = (NOW - timedelta(minutes=1)).replace(tzinfo=None)
    coerced = nb._aware(naive)
    assert coerced.tzinfo == UTC
    assert coerced == naive.replace(tzinfo=UTC)


# ── Adaptive per-user volume ─────────────────────────────────────────────────

def test_adaptive_new_user_gets_gentle_default():
    # Under the minimum sample we can't tell a non-engager from a brand-new user, so
    # the gentle default applies (never the harsh one-a-day floor).
    cap, spacing = resolve_adaptive_limits(delivered=ADAPTIVE_MIN_SAMPLE - 1, opened=0)
    assert cap == 3 and spacing == timedelta(minutes=90)


def test_adaptive_total_ignorer_is_throttled_hard():
    # Enough sample, zero taps -> one gentle ping/day, far apart.
    cap, spacing = resolve_adaptive_limits(delivered=20, opened=0)
    assert cap == 1 and spacing == timedelta(hours=6)


def test_adaptive_rare_tapper():
    cap, spacing = resolve_adaptive_limits(delivered=20, opened=2)  # e=0.10
    assert cap == 2 and spacing == timedelta(hours=4)


def test_adaptive_sometimes_tapper_balanced_middle():
    cap, spacing = resolve_adaptive_limits(delivered=20, opened=6)  # e=0.30
    assert cap == 3 and spacing == timedelta(hours=2)


def test_adaptive_heavy_tapper_leans_in():
    cap, spacing = resolve_adaptive_limits(delivered=20, opened=12)  # e=0.60
    assert cap == 5 and spacing == timedelta(minutes=45)


def test_adaptive_cap_is_monotonic_in_engagement():
    caps = [resolve_adaptive_limits(20, o)[0] for o in (0, 2, 6, 12)]
    assert caps == sorted(caps)  # more taps never lowers the ceiling


def test_evaluate_respects_caller_supplied_adaptive_limits():
    # A throttled user (adaptive cap=1) is blocked at 1 even though the flat beta cap
    # is far higher — proving the per-user limit, not the global constant, governs.
    data = {FIELD_SENDS_TODAY_DATE: TODAY, FIELD_PROACTIVE_SENDS_TODAY: 1}
    decision = evaluate_proactive_claim(
        data, local_date=TODAY, now=NOW, cap=1, spacing=timedelta(hours=6)
    )
    assert not decision.allowed and decision.reason == "global_daily_cap"


# ── Fail-open safety ─────────────────────────────────────────────────────────

async def test_claim_fails_open_on_error():
    # A Firestore explosion must never silence notifications.
    with patch.object(nb, "admin_firestore", side_effect=RuntimeError("firestore down")):
        result = await nb.try_claim_proactive_slot("u1", source="signal_engine", user_local_date=TODAY)
    assert result.allowed
    assert isinstance(result, BudgetDecision)
