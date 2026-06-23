"""
Writer/reader round-trip contract for the unified notification ledger.

Guards the field-name contract (CLAUDE.md data-layer discipline): if a FIELD_*
constant is renamed without updating the doc shape, or the tap resolver stops
computing the send->tap latency, these break instead of silently writing rows a
future reader can't query.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock, patch

import pytest

from src.services import notification_ledger as nl


def _mock_db_with_doc_ref(doc_ref: MagicMock) -> MagicMock:
    """Build a Firestore mock whose users/{uid}/notifications/{id} chain resolves
    to ``doc_ref`` regardless of the ids passed (MagicMock shares return_value)."""
    mock_db = MagicMock()
    (
        mock_db.collection.return_value
        .document.return_value
        .collection.return_value
        .document.return_value
    ) = doc_ref
    return mock_db


@pytest.mark.asyncio
async def test_record_send_writes_core_and_decision():
    captured: dict = {}
    doc_ref = MagicMock()
    doc_ref.set.side_effect = lambda d: captured.update(doc=d)
    mock_db = _mock_db_with_doc_ref(doc_ref)

    with patch.object(nl, "admin_firestore", return_value=mock_db):
        await nl.record_send(
            "u1",
            notification_id="n1",
            notification_type="signal_engine",
            origin="signal_engine",
            title="Kohli did it again",
            body="112 off 98",
            url="https://news.google.com/rss/articles/abc",
            content_id="google_news_x",
            source="google_news",
            category="sports",
            content_kind="read",
            delivered=True,
            tokens_targeted=2,
            success_count=2,
            failure_count=0,
            decision=nl.NotificationDecision(
                score=0.78,
                components={"cosine": 0.71, "slot": 1.2, "freshness": 0.95},
                gate_a_active=True,
                matched_interest_slug="sports",
                relevance_reason="cricket / Kohli",
                framer_prompt_version="2026-06-17",
                sends_today_before=1,
                local_hour=20,
                day_of_week=2,
            ),
        )

    doc = captured["doc"]
    # Core fields the user asked to see: url, type, source, time sent, reason.
    assert doc[nl.FIELD_NOTIFICATION_ID] == "n1"
    assert doc[nl.FIELD_TYPE] == "signal_engine"
    assert doc[nl.FIELD_URL] == "https://news.google.com/rss/articles/abc"
    assert doc[nl.FIELD_SOURCE] == "google_news"
    assert isinstance(doc[nl.FIELD_SENT_AT], datetime)
    assert doc[nl.FIELD_STATUS] == nl.STATUS_SENT
    assert doc[nl.FIELD_OUTCOME] == nl.OUTCOME_PENDING
    assert doc[nl.FIELD_TAPPED_AT] is None
    assert doc[nl.FIELD_TIME_TO_TAP_SECONDS] is None
    assert doc[nl.FIELD_LED_TO_SESSION] is False
    assert doc[nl.FIELD_DELIVERY]["delivered"] is True
    assert doc[nl.FIELD_EXPIRES_AT] > doc[nl.FIELD_SENT_AT]
    # Decision sub-map (the learning substrate).
    decision = doc[nl.FIELD_DECISION]
    assert decision["score"] == 0.78
    assert decision["components"]["cosine"] == 0.71
    assert decision["relevance_reason"] == "cricket / Kohli"
    assert decision["matched_interest_slug"] == "sports"
    assert decision["local_hour"] == 20


@pytest.mark.asyncio
async def test_record_send_failed_delivery_has_no_decision():
    captured: dict = {}
    doc_ref = MagicMock()
    doc_ref.set.side_effect = lambda d: captured.update(doc=d)
    mock_db = _mock_db_with_doc_ref(doc_ref)

    with patch.object(nl, "admin_firestore", return_value=mock_db):
        await nl.record_send(
            "u1",
            notification_id="n2",
            notification_type="reminder",
            origin="reminder",
            title="Buddy Reminder",
            body="Rental application",
            delivered=False,
            tokens_targeted=1,
            success_count=0,
            failure_count=1,
        )

    doc = captured["doc"]
    assert doc[nl.FIELD_STATUS] == nl.STATUS_FAILED
    assert doc[nl.FIELD_DELIVERY]["delivered"] is False
    assert doc[nl.FIELD_DECISION] is None  # deterministic path, no learning data


@pytest.mark.asyncio
async def test_record_tap_sets_tapped_at_and_latency():
    sent = datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC)
    tapped = datetime(2026, 6, 17, 12, 0, 30, tzinfo=UTC)

    snap = MagicMock()
    snap.exists = True
    snap.to_dict.return_value = {
        nl.FIELD_SENT_AT: sent,
        nl.FIELD_TAPPED_AT: None,
        nl.FIELD_OUTCOME: nl.OUTCOME_PENDING,
    }
    captured: dict = {}
    doc_ref = MagicMock()
    doc_ref.get.return_value = snap
    doc_ref.update.side_effect = lambda u: captured.update(u)
    mock_db = _mock_db_with_doc_ref(doc_ref)

    with patch.object(nl, "admin_firestore", return_value=mock_db):
        await nl.record_tap("u1", "n1", tapped_at=tapped)

    assert captured[nl.FIELD_TAPPED_AT] == tapped
    assert captured[nl.FIELD_TIME_TO_TAP_SECONDS] == 30.0
    assert captured[nl.FIELD_OUTCOME] == nl.OUTCOME_OPENED
    assert captured[nl.FIELD_LED_TO_SESSION] is True


@pytest.mark.asyncio
async def test_record_tap_is_idempotent():
    snap = MagicMock()
    snap.exists = True
    snap.to_dict.return_value = {
        nl.FIELD_SENT_AT: datetime(2026, 6, 17, 12, 0, 0, tzinfo=UTC),
        nl.FIELD_TAPPED_AT: datetime(2026, 6, 17, 12, 0, 5, tzinfo=UTC),
        nl.FIELD_OUTCOME: nl.OUTCOME_OPENED,
    }
    doc_ref = MagicMock()
    doc_ref.get.return_value = snap
    mock_db = _mock_db_with_doc_ref(doc_ref)

    with patch.object(nl, "admin_firestore", return_value=mock_db):
        await nl.record_tap("u1", "n1")

    doc_ref.update.assert_not_called()


def _snap(status: str, outcome: str) -> MagicMock:
    s = MagicMock()
    s.to_dict.return_value = {nl.FIELD_STATUS: status, nl.FIELD_OUTCOME: outcome}
    return s


@pytest.mark.asyncio
async def test_recent_engagement_counts_only_delivered_and_opened():
    # delivered+opened, delivered+pending, delivered+dismissed, and a FAILED row.
    rows = [
        _snap(nl.STATUS_SENT, nl.OUTCOME_OPENED),
        _snap(nl.STATUS_SENT, nl.OUTCOME_OPENED),
        _snap(nl.STATUS_SENT, nl.OUTCOME_PENDING),
        _snap(nl.STATUS_SENT, nl.OUTCOME_DISMISSED),
        _snap(nl.STATUS_FAILED, nl.OUTCOME_PENDING),  # never seen → not counted at all
    ]
    mock_db = MagicMock()
    stream_chain = (
        mock_db.collection.return_value
        .document.return_value
        .collection.return_value
        .where.return_value
        .limit.return_value
    )
    stream_chain.stream.return_value = rows

    with patch.object(nl, "admin_firestore", return_value=mock_db):
        delivered, opened = await nl.recent_engagement(
            "u1", since=datetime(2026, 6, 1, tzinfo=UTC)
        )

    assert delivered == 4   # the 4 SENT rows; the FAILED one is excluded
    assert opened == 2      # only the two OUTCOME_OPENED


@pytest.mark.asyncio
async def test_recent_engagement_fails_open_to_zero():
    with patch.object(nl, "admin_firestore", side_effect=RuntimeError("firestore down")):
        delivered, opened = await nl.recent_engagement(
            "u1", since=datetime(2026, 6, 1, tzinfo=UTC)
        )
    assert (delivered, opened) == (0, 0)


@pytest.mark.asyncio
async def test_record_dismiss_only_flips_pending():
    snap = MagicMock()
    snap.exists = True
    snap.to_dict.return_value = {nl.FIELD_OUTCOME: nl.OUTCOME_OPENED}  # already tapped
    doc_ref = MagicMock()
    doc_ref.get.return_value = snap
    mock_db = _mock_db_with_doc_ref(doc_ref)

    with patch.object(nl, "admin_firestore", return_value=mock_db):
        await nl.record_dismiss("u1", "n1")

    doc_ref.update.assert_not_called()
