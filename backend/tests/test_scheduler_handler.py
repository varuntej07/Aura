"""
Tests for src/handlers/scheduler.py

Covers: handle_scheduler_tick — all branches including delivery, no-delivery, per-item
exception, and outer exception.

GoogleCalendarConnector is imported inside handle_scheduler_tick's function body, so it
must be patched at its source module rather than at src.handlers.scheduler.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

_GCC_PATH = "src.services.google_calendar_connector.GoogleCalendarConnector"


def _make_send_result(delivered: bool, tokens_targeted: int = 1, success_count: int = 1):
    result = MagicMock()
    result.delivered = delivered
    result.tokens_targeted = tokens_targeted
    result.success_count = success_count
    return result


def _patch_gc(renew=0, sync=0):
    """Context manager that patches GoogleCalendarConnector class methods."""
    mock_cls = MagicMock()
    mock_cls.renew_expiring_channels = MagicMock(return_value=renew)
    mock_cls.process_pending_sync_jobs = MagicMock(return_value=sync)
    return patch(_GCC_PATH, mock_cls)


class TestHandleSchedulerTick:
    @pytest.mark.asyncio
    async def test_no_due_reminders_returns_scanned_zero(self):
        from src.handlers.scheduler import handle_scheduler_tick

        with patch("src.handlers.scheduler.fetch_due_reminders", return_value=[]):
            with _patch_gc():
                result = await handle_scheduler_tick()

        assert result["statusCode"] == 200
        body = json.loads(result["body"])
        assert body["scanned"] == 0
        assert body["delivered"] == 0

    @pytest.mark.asyncio
    async def test_due_reminder_delivered_marks_fired(self):
        from src.handlers.scheduler import handle_scheduler_tick

        due = [{"userId": "u1", "reminderId": "r1", "data": {"message": "Take meds"}}]
        send_result = _make_send_result(delivered=True)

        with patch("src.handlers.scheduler.fetch_due_reminders", return_value=due):
            with patch("src.handlers.scheduler.claim_reminder_for_processing", return_value=True):
                with patch("src.handlers.scheduler.mark_reminder_fired") as mock_mark:
                    with patch("src.handlers.scheduler.rewrite_reminder_notification", new=AsyncMock(return_value="Take meds")):
                        with patch("src.handlers.scheduler.send_notification", new=AsyncMock(return_value=send_result)):
                            with _patch_gc():
                                result = await handle_scheduler_tick()

        body = json.loads(result["body"])
        assert body["scanned"] == 1
        assert body["delivered"] == 1
        mock_mark.assert_called_once_with("u1", "r1")

    @pytest.mark.asyncio
    async def test_due_reminder_not_delivered_does_not_mark_fired(self):
        from src.handlers.scheduler import handle_scheduler_tick

        due = [{"userId": "u1", "reminderId": "r1", "data": {"message": "Stand up"}}]
        send_result = _make_send_result(delivered=False, tokens_targeted=0, success_count=0)

        with patch("src.handlers.scheduler.fetch_due_reminders", return_value=due):
            with patch("src.handlers.scheduler.mark_reminder_fired") as mock_mark:
                with patch("src.handlers.scheduler.rewrite_reminder_notification", new=AsyncMock(return_value="Stand up")):
                    with patch("src.handlers.scheduler.send_notification", new=AsyncMock(return_value=send_result)):
                        with _patch_gc():
                            result = await handle_scheduler_tick()

        body = json.loads(result["body"])
        assert body["delivered"] == 0
        mock_mark.assert_not_called()

    @pytest.mark.asyncio
    async def test_per_reminder_exception_is_caught_loop_continues(self):
        from src.handlers.scheduler import handle_scheduler_tick

        due = [
            {"userId": "u1", "reminderId": "r_fail", "data": {"message": "Bad one"}},
            {"userId": "u2", "reminderId": "r_ok", "data": {"message": "Good one"}},
        ]
        good_result = _make_send_result(delivered=True)

        async def send_side_effect(user_id, **kwargs):
            if user_id == "u1":
                raise RuntimeError("FCM exploded")
            return good_result

        with patch("src.handlers.scheduler.fetch_due_reminders", return_value=due):
            with patch("src.handlers.scheduler.claim_reminder_for_processing", return_value=True):
                with patch("src.handlers.scheduler.mark_reminder_fired") as mock_mark:
                    with patch("src.handlers.scheduler.rewrite_reminder_notification", new=AsyncMock(return_value="msg")):
                        with patch("src.handlers.scheduler.send_notification", new=AsyncMock(side_effect=send_side_effect)):
                            with _patch_gc():
                                result = await handle_scheduler_tick()

        body = json.loads(result["body"])
        assert body["scanned"] == 2
        assert body["delivered"] == 1
        mock_mark.assert_called_once_with("u2", "r_ok")

    @pytest.mark.asyncio
    async def test_outer_exception_returns_500(self):
        from src.handlers.scheduler import handle_scheduler_tick

        with patch("src.handlers.scheduler.fetch_due_reminders", side_effect=Exception("db down")):
            with _patch_gc():
                result = await handle_scheduler_tick()

        assert result["statusCode"] == 500
        body = json.loads(result["body"])
        assert "error" in body

    @pytest.mark.asyncio
    async def test_missing_message_field_defaults_to_reminder_due_now(self):
        """data dict with no 'message' key should not crash."""
        from src.handlers.scheduler import handle_scheduler_tick

        due = [{"userId": "u1", "reminderId": "r1", "data": {}}]
        send_result = _make_send_result(delivered=True)

        with patch("src.handlers.scheduler.fetch_due_reminders", return_value=due):
            with patch("src.handlers.scheduler.claim_reminder_for_processing", return_value=True):
                with patch("src.handlers.scheduler.mark_reminder_fired"):
                    with patch("src.handlers.scheduler.rewrite_reminder_notification", new=AsyncMock(return_value="Reminder due now")) as mock_rw:
                        with patch("src.handlers.scheduler.send_notification", new=AsyncMock(return_value=send_result)):
                            with _patch_gc():
                                await handle_scheduler_tick()

        mock_rw.assert_called_once_with("Reminder due now")
