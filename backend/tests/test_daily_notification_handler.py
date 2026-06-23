"""
Tests for src/handlers/daily_notification.py

Covers:
  - handle_send_nudge (all response branches)
  - _load_daily_plan (exists, missing, exception)
  - _update_nudge_status (success, exception)
  - _update_engagement_guard (success, exception)
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# handle_send_nudge
# ---------------------------------------------------------------------------

def _make_nudge_plan(status="scheduled"):
    return {
        "morning_nudge": {
            "title": "Morning title",
            "body": "Morning body",
            "opening_chat_message": "Hello",
            "quick_reply_chips": ["OK", "Later"],
            "status": status,
        },
        "evening_nudge": {
            "title": "Evening title",
            "body": "Evening body",
            "opening_chat_message": "Good evening",
            "quick_reply_chips": ["Great", "Tired"],
            "status": "scheduled",
        },
    }


def _make_decision(delivered: bool, tokens_targeted: int = 1, success_count: int = 1, failure_count: int = 0):
    """Build the OrchestratorDecision the committed lane now returns to the handler."""
    from src.services.notifications.proposal import Disposition, OrchestratorDecision

    return OrchestratorDecision(
        Disposition.SEND, "ok",
        delivered=delivered, tokens_targeted=tokens_targeted,
        success_count=success_count, failure_count=failure_count,
    )


class TestHandleSendNudge:
    @pytest.mark.asyncio
    async def test_missing_user_id_returns_400(self):
        from src.handlers.daily_notification import handle_send_nudge
        result = await handle_send_nudge({"plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})
        assert result["error"] == "invalid_payload"
        assert result["status_code"] == 400

    @pytest.mark.asyncio
    async def test_missing_plan_date_returns_400(self):
        from src.handlers.daily_notification import handle_send_nudge
        result = await handle_send_nudge({"user_id": "u1", "nudge_slot": "morning_nudge"})
        assert result["error"] == "invalid_payload"

    @pytest.mark.asyncio
    async def test_invalid_nudge_slot_returns_400(self):
        from src.handlers.daily_notification import handle_send_nudge
        result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "bad_slot"})
        assert result["error"] == "invalid_payload"

    @pytest.mark.asyncio
    async def test_plan_not_found_returns_503(self):
        from src.handlers.daily_notification import handle_send_nudge

        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=None)):
            result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["error"] == "plan_not_found"
        assert result["status_code"] == 503

    @pytest.mark.asyncio
    async def test_already_sent_returns_skipped(self):
        from src.handlers.daily_notification import handle_send_nudge

        plan = _make_nudge_plan(status="sent")
        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["skipped"] is True
        assert result["reason"] == "already_sent"

    @pytest.mark.asyncio
    async def test_missing_title_returns_400(self):
        from src.handlers.daily_notification import handle_send_nudge

        plan = {"morning_nudge": {"title": "", "body": "Something", "status": "scheduled"}}
        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["error"] == "missing_content"

    @pytest.mark.asyncio
    async def test_missing_body_returns_400(self):
        from src.handlers.daily_notification import handle_send_nudge

        plan = {"morning_nudge": {"title": "Title", "body": "", "status": "scheduled"}}
        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["error"] == "missing_content"

    @pytest.mark.asyncio
    async def test_no_tokens_returns_no_devices(self):
        from src.handlers.daily_notification import handle_send_nudge

        plan = _make_nudge_plan()
        decision = _make_decision(delivered=False, tokens_targeted=0, success_count=0)

        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            with patch("src.handlers.daily_notification.orchestrator.submit", new=AsyncMock(return_value=decision)):
                result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["status"] == "no_devices"
        assert result["tokens_targeted"] == 0

    @pytest.mark.asyncio
    async def test_fcm_delivery_failed_returns_500(self):
        from src.handlers.daily_notification import handle_send_nudge

        plan = _make_nudge_plan()
        decision = _make_decision(delivered=False, tokens_targeted=2, success_count=0, failure_count=2)

        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            with patch("src.handlers.daily_notification.orchestrator.submit", new=AsyncMock(return_value=decision)):
                result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["error"] == "fcm_delivery_failed"
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_successful_send_updates_status_and_guard(self):
        from src.handlers.daily_notification import handle_send_nudge

        plan = _make_nudge_plan()
        decision = _make_decision(delivered=True, tokens_targeted=1, success_count=1)

        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            with patch("src.handlers.daily_notification.orchestrator.submit", new=AsyncMock(return_value=decision)):
                with patch("src.handlers.daily_notification._update_nudge_status", new=AsyncMock()) as mock_status:
                    with patch("src.handlers.daily_notification._update_engagement_guard", new=AsyncMock()) as mock_guard:
                        result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["status"] == "sent"
        assert result["tokens_targeted"] == 1
        mock_status.assert_called_once_with("u1", "2026-05-02", "morning_nudge", "sent", mock_status.call_args[0][4])
        mock_guard.assert_called_once()


# ---------------------------------------------------------------------------
# _load_daily_plan
# ---------------------------------------------------------------------------

class TestLoadDailyPlan:
    @pytest.mark.asyncio
    async def test_returns_dict_when_exists(self):
        from src.handlers.daily_notification import _load_daily_plan

        doc = MagicMock()
        doc.exists = True
        doc.to_dict = MagicMock(return_value={"plan_date": "2026-05-02"})

        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = doc

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            result = await _load_daily_plan("u1", "2026-05-02")

        assert result is not None
        assert result["plan_date"] == "2026-05-02"

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self):
        from src.handlers.daily_notification import _load_daily_plan

        doc = MagicMock()
        doc.exists = False

        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value.get.return_value = doc

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            result = await _load_daily_plan("u1", "2026-05-02")

        assert result is None

    @pytest.mark.asyncio
    async def test_exception_returns_none(self):
        from src.handlers.daily_notification import _load_daily_plan

        with patch("src.handlers.daily_notification.admin_firestore", side_effect=Exception("db")):
            result = await _load_daily_plan("u1", "2026-05-02")

        assert result is None


# ---------------------------------------------------------------------------
# _update_nudge_status
# ---------------------------------------------------------------------------

class TestUpdateNudgeStatus:
    @pytest.mark.asyncio
    async def test_calls_update_on_firestore(self):
        from src.handlers.daily_notification import _update_nudge_status

        plan_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = plan_ref

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            await _update_nudge_status("u1", "2026-05-02", "morning_nudge", "sent", "2026-05-02T08:31:00Z")

        plan_ref.update.assert_called_once()
        args, _ = plan_ref.update.call_args
        payload: dict = args[0]
        assert payload["morning_nudge.status"] == "sent"
        assert payload["morning_nudge.sent_at"] == "2026-05-02T08:31:00Z"

    @pytest.mark.asyncio
    async def test_exception_is_swallowed(self):
        from src.handlers.daily_notification import _update_nudge_status

        with patch("src.handlers.daily_notification.admin_firestore", side_effect=Exception("db")):
            # Should not raise
            await _update_nudge_status("u1", "2026-05-02", "morning_nudge", "sent", "ts")


# ---------------------------------------------------------------------------
# _update_engagement_guard
# ---------------------------------------------------------------------------

class TestUpdateEngagementGuard:
    @pytest.mark.asyncio
    async def test_transaction_increments_count(self):
        """The inner Firestore transaction must increment proactive_notifications_sent_today."""
        from src.handlers.daily_notification import _update_engagement_guard

        # Build Firestore mock with a transaction that runs the callback inline
        guard_doc = MagicMock()
        guard_doc.exists = True
        from datetime import datetime, timezone
        today = datetime.now(timezone.utc).date().isoformat()
        guard_doc.to_dict = MagicMock(return_value={
            "guard_date": today,
            "proactive_notifications_sent_today": 1,
            "last_engaged_at": f"{today}T07:00:00Z",
        })

        guard_ref = MagicMock()
        guard_ref.get = MagicMock(return_value=guard_doc)

        eng_col = MagicMock()
        eng_col.document.return_value = guard_ref

        user_ref = MagicMock()
        user_ref.collection.return_value = eng_col

        users_col = MagicMock()
        users_col.document.return_value = user_ref

        mock_txn = MagicMock()

        db = MagicMock()
        db.collection.return_value = users_col
        db.transaction.return_value = mock_txn

        # Make fs.transactional a pass-through decorator and call the wrapped fn inline
        def fake_transactional(fn):
            def wrapper(txn):
                return fn(txn)
            return wrapper

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            with patch("google.cloud.firestore.transactional", side_effect=fake_transactional):
                await _update_engagement_guard("u1", "2026-05-03T08:31:00Z")

        # Transaction.set must have been called
        mock_txn.set.assert_called_once()
        payload = mock_txn.set.call_args[0][1]
        assert payload["proactive_notifications_sent_today"] == 2

    @pytest.mark.asyncio
    async def test_exception_is_swallowed(self):
        from src.handlers.daily_notification import _update_engagement_guard

        with patch("src.handlers.daily_notification.admin_firestore", side_effect=Exception("db")):
            await _update_engagement_guard("u1", "2026-05-02T08:31:00Z")
