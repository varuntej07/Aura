"""Tests for the priority reserved slot added for the icebreaker.

A normal proactive claim is capped at GLOBAL_DAILY_PROACTIVE_CAP; a priority claim
(the icebreaker) may use one reserved slot above it, so the every-15-min content
engine can never starve the once-a-day personal opener. Spacing is unaffected.
"""

from __future__ import annotations

from datetime import UTC, datetime

from src.services.notification_budget import (
    GLOBAL_DAILY_PROACTIVE_CAP,
    PRIORITY_RESERVED_SLOTS,
    FIELD_PROACTIVE_SENDS_TODAY,
    FIELD_SENDS_TODAY_DATE,
    evaluate_proactive_claim,
)

_DATE = "2026-06-12"
_NOW = datetime(2026, 6, 12, 18, 0, tzinfo=UTC)


def _doc_at_cap() -> dict:
    return {
        FIELD_SENDS_TODAY_DATE: _DATE,
        FIELD_PROACTIVE_SENDS_TODAY: GLOBAL_DAILY_PROACTIVE_CAP,
    }


def test_normal_claim_blocked_at_cap():
    decision = evaluate_proactive_claim(_doc_at_cap(), local_date=_DATE, now=_NOW)
    assert decision.allowed is False
    assert decision.reason == "global_daily_cap"


def test_priority_claim_allowed_in_reserved_slot_at_cap():
    decision = evaluate_proactive_claim(
        _doc_at_cap(), local_date=_DATE, now=_NOW, priority=True
    )
    assert decision.allowed is True


def test_priority_claim_blocked_once_reserved_slot_used():
    doc = {
        FIELD_SENDS_TODAY_DATE: _DATE,
        FIELD_PROACTIVE_SENDS_TODAY: GLOBAL_DAILY_PROACTIVE_CAP + PRIORITY_RESERVED_SLOTS,
    }
    decision = evaluate_proactive_claim(doc, local_date=_DATE, now=_NOW, priority=True)
    assert decision.allowed is False
