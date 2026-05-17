"""
Tests for src/handlers/daily_notification.py

Covers:
  - _seconds_until_7am_local (all timezone branches)
  - handle_plan_all_users (no users, success, enqueue error)
  - handle_plan_one_user
  - handle_send_nudge (all response branches)
  - _fetch_active_user_ids (no tokens, recent query, no-query new account, exception)
  - _fetch_user_timezone (exists, missing, exception)
  - _load_daily_plan (exists, missing, exception)
  - _update_nudge_status (success, exception)
  - _update_engagement_guard (success, exception)
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, call
from zoneinfo import ZoneInfo

import pytest


# ---------------------------------------------------------------------------
# _seconds_until_7am_local
# ---------------------------------------------------------------------------

class TestSecondsUntil7am:
    def test_before_7am_returns_positive_delay(self):
        from src.handlers.daily_notification import _seconds_until_7am_local
        # Simulate now = 06:00 America/New_York
        tz = "America/New_York"
        ny = ZoneInfo(tz)
        now = datetime.now(ny).replace(hour=6, minute=0, second=0, microsecond=0)
        with patch("src.handlers.daily_notification.datetime") as mock_dt:
            mock_dt.now = MagicMock(return_value=now)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            delay = _seconds_until_7am_local(tz)
        assert 0 < delay <= 3600  # exactly 1 hour away

    def test_after_7am_schedules_for_tomorrow(self):
        from src.handlers.daily_notification import _seconds_until_7am_local
        tz = "America/New_York"
        ny = ZoneInfo(tz)
        now = datetime.now(ny).replace(hour=10, minute=0, second=0, microsecond=0)
        with patch("src.handlers.daily_notification.datetime") as mock_dt:
            mock_dt.now = MagicMock(return_value=now)
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
            delay = _seconds_until_7am_local(tz)
        # From 10am to next 7am is 21 hours = 75600s
        assert 70000 < delay <= 86400

    def test_invalid_timezone_falls_back_to_utc(self):
        from src.handlers.daily_notification import _seconds_until_7am_local
        # Should not raise; just uses UTC
        delay = _seconds_until_7am_local("Invalid/Timezone")
        assert 0 <= delay <= 86400


# ---------------------------------------------------------------------------
# handle_plan_all_users
# ---------------------------------------------------------------------------

class TestHandlePlanAllUsers:
    @pytest.mark.asyncio
    async def test_no_active_users_returns_scheduled_zero(self):
        from src.handlers.daily_notification import handle_plan_all_users

        with patch("src.handlers.daily_notification._fetch_active_user_ids", new=AsyncMock(return_value=[])):
            result = await handle_plan_all_users()

        assert result["scheduled"] == 0

    @pytest.mark.asyncio
    async def test_one_user_enqueues_task(self):
        from src.handlers.daily_notification import handle_plan_all_users

        with patch("src.handlers.daily_notification._fetch_active_user_ids", new=AsyncMock(return_value=["uid1"])):
            with patch("src.handlers.daily_notification._fetch_user_timezone", new=AsyncMock(return_value="UTC")):
                with patch("src.handlers.daily_notification._enqueue_plan_task") as mock_enqueue:
                    result = await handle_plan_all_users()

        assert result["scheduled"] == 1
        assert result["errors"] == 0
        mock_enqueue.assert_called_once()

    @pytest.mark.asyncio
    async def test_enqueue_failure_counts_as_error(self):
        from src.handlers.daily_notification import handle_plan_all_users

        with patch("src.handlers.daily_notification._fetch_active_user_ids", new=AsyncMock(return_value=["uid1"])):
            with patch("src.handlers.daily_notification._fetch_user_timezone", new=AsyncMock(return_value="UTC")):
                with patch("src.handlers.daily_notification._enqueue_plan_task", side_effect=Exception("tasks api down")):
                    result = await handle_plan_all_users()

        assert result["scheduled"] == 0
        assert result["errors"] == 1


# ---------------------------------------------------------------------------
# handle_plan_one_user
# ---------------------------------------------------------------------------

class TestHandlePlanOneUser:
    @pytest.mark.asyncio
    async def test_calls_run_daily_plan_and_returns_ok(self):
        from src.handlers.daily_notification import handle_plan_one_user

        with patch("src.handlers.daily_notification.run_daily_plan", new=AsyncMock()) as mock_plan:
            result = await handle_plan_one_user("uid1")

        mock_plan.assert_called_once_with("uid1")
        assert result["status"] == "ok"
        assert result["user_id"] == "uid1"


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


def _make_send_result(delivered: bool, tokens_targeted: int = 1, success_count: int = 1, failure_count: int = 0):
    r = MagicMock()
    r.delivered = delivered
    r.tokens_targeted = tokens_targeted
    r.success_count = success_count
    r.failure_count = failure_count
    return r


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
        send_result = _make_send_result(delivered=False, tokens_targeted=0, success_count=0)

        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            with patch("src.handlers.daily_notification.send_notification", new=AsyncMock(return_value=send_result)):
                result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["status"] == "no_devices"
        assert result["tokens_targeted"] == 0

    @pytest.mark.asyncio
    async def test_fcm_delivery_failed_returns_500(self):
        from src.handlers.daily_notification import handle_send_nudge

        plan = _make_nudge_plan()
        send_result = _make_send_result(delivered=False, tokens_targeted=2, success_count=0, failure_count=2)

        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            with patch("src.handlers.daily_notification.send_notification", new=AsyncMock(return_value=send_result)):
                result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["error"] == "fcm_delivery_failed"
        assert result["status_code"] == 500

    @pytest.mark.asyncio
    async def test_successful_send_updates_status_and_guard(self):
        from src.handlers.daily_notification import handle_send_nudge

        plan = _make_nudge_plan()
        send_result = _make_send_result(delivered=True, tokens_targeted=1, success_count=1)

        with patch("src.handlers.daily_notification._load_daily_plan", new=AsyncMock(return_value=plan)):
            with patch("src.handlers.daily_notification.send_notification", new=AsyncMock(return_value=send_result)):
                with patch("src.handlers.daily_notification._update_nudge_status", new=AsyncMock()) as mock_status:
                    with patch("src.handlers.daily_notification._update_engagement_guard", new=AsyncMock()) as mock_guard:
                        result = await handle_send_nudge({"user_id": "u1", "plan_date": "2026-05-02", "nudge_slot": "morning_nudge"})

        assert result["status"] == "sent"
        assert result["tokens_targeted"] == 1
        mock_status.assert_called_once_with("u1", "2026-05-02", "morning_nudge", "sent", mock_status.call_args[0][4])
        mock_guard.assert_called_once()


# ---------------------------------------------------------------------------
# _fetch_active_user_ids
# ---------------------------------------------------------------------------

class TestFetchActiveUserIds:
    @pytest.mark.asyncio
    async def test_no_token_docs_returns_empty(self):
        from src.handlers.daily_notification import _fetch_active_user_ids

        db = MagicMock()
        db.collection_group.return_value.stream.return_value = []

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            result = await _fetch_active_user_ids()

        assert result == []

    @pytest.mark.asyncio
    async def test_user_with_recent_queries_is_active(self):
        from src.handlers.daily_notification import _fetch_active_user_ids

        # Build token doc pointing to users/{uid}/fcm_tokens/{token}
        token_doc = MagicMock()
        parent_parent = MagicMock()
        parent_parent.id = "uid1"
        token_doc.reference.parent.parent = parent_parent
        token_doc.reference.path = "users/uid1/fcm_tokens/tok"

        # queries subcollection returns one recent doc
        recent_query_doc = MagicMock()
        query_col = MagicMock()
        query_col.where.return_value.limit.return_value.stream.return_value = iter([recent_query_doc])

        user_doc_ref = MagicMock()
        user_doc_ref.collection.return_value = query_col

        users_col = MagicMock()
        users_col.document.return_value = user_doc_ref

        db = MagicMock()
        db.collection_group.return_value.stream.return_value = iter([token_doc])
        db.collection.return_value = users_col

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            with patch("google.cloud.firestore_v1.base_query.FieldFilter"):
                result = await _fetch_active_user_ids()

        assert "uid1" in result

    @pytest.mark.asyncio
    async def test_user_with_no_queries_ever_is_treated_as_new_account(self):
        """Users with no query docs at all (new accounts) must be included (lines 222-228)."""
        from src.handlers.daily_notification import _fetch_active_user_ids

        token_doc = MagicMock()
        parent_parent = MagicMock()
        parent_parent.id = "new_uid"
        token_doc.reference.parent.parent = parent_parent
        token_doc.reference.path = "users/new_uid/fcm_tokens/tok"

        # recent query filter returns nothing (no recent queries)
        # all-query stream also returns nothing (no queries ever)
        query_col = MagicMock()
        query_col.where.return_value.limit.return_value.stream.return_value = iter([])
        query_col.limit.return_value.stream.return_value = iter([])

        user_doc_ref = MagicMock()
        user_doc_ref.collection.return_value = query_col

        users_col = MagicMock()
        users_col.document.return_value = user_doc_ref

        db = MagicMock()
        db.collection_group.return_value.stream.return_value = iter([token_doc])
        db.collection.return_value = users_col

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            result = await _fetch_active_user_ids()

        assert "new_uid" in result

    @pytest.mark.asyncio
    async def test_user_with_only_old_queries_is_excluded(self):
        """Users whose most recent queries are older than 7 days must be excluded."""
        from src.handlers.daily_notification import _fetch_active_user_ids

        token_doc = MagicMock()
        parent_parent = MagicMock()
        parent_parent.id = "inactive_uid"
        token_doc.reference.parent.parent = parent_parent
        token_doc.reference.path = "users/inactive_uid/fcm_tokens/tok"

        old_query_doc = MagicMock()  # returned by all-queries stream (user HAS queries, just old)
        query_col = MagicMock()
        # recent filter → empty (no recent queries)
        query_col.where.return_value.limit.return_value.stream.return_value = iter([])
        # all queries → has docs (not a new account)
        query_col.limit.return_value.stream.return_value = iter([old_query_doc])

        user_doc_ref = MagicMock()
        user_doc_ref.collection.return_value = query_col

        users_col = MagicMock()
        users_col.document.return_value = user_doc_ref

        db = MagicMock()
        db.collection_group.return_value.stream.return_value = iter([token_doc])
        db.collection.return_value = users_col

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            result = await _fetch_active_user_ids()

        assert "inactive_uid" not in result

    @pytest.mark.asyncio
    async def test_exception_returns_empty_list(self):
        from src.handlers.daily_notification import _fetch_active_user_ids

        with patch("src.handlers.daily_notification.admin_firestore", side_effect=Exception("db error")):
            result = await _fetch_active_user_ids()

        assert result == []


# ---------------------------------------------------------------------------
# _fetch_user_timezone
# ---------------------------------------------------------------------------

class TestFetchUserTimezone:
    @pytest.mark.asyncio
    async def test_returns_timezone_from_doc(self):
        from src.handlers.daily_notification import _fetch_user_timezone

        doc = MagicMock()
        doc.exists = True
        doc.to_dict = MagicMock(return_value={"timezone": "America/Chicago"})

        db = MagicMock()
        db.collection.return_value.document.return_value.get.return_value = doc

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            tz = await _fetch_user_timezone("uid1")

        assert tz == "America/Chicago"

    @pytest.mark.asyncio
    async def test_missing_doc_returns_utc(self):
        from src.handlers.daily_notification import _fetch_user_timezone

        doc = MagicMock()
        doc.exists = False

        db = MagicMock()
        db.collection.return_value.document.return_value.get.return_value = doc

        with patch("src.handlers.daily_notification.admin_firestore", return_value=db):
            tz = await _fetch_user_timezone("uid1")

        assert tz == "UTC"

    @pytest.mark.asyncio
    async def test_exception_returns_utc(self):
        from src.handlers.daily_notification import _fetch_user_timezone

        with patch("src.handlers.daily_notification.admin_firestore", side_effect=Exception("boom")):
            tz = await _fetch_user_timezone("uid1")

        assert tz == "UTC"


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
        from datetime import date
        today = date.today().isoformat()
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


class TestEnqueuePlanTask:
    def test_with_zero_delay_no_schedule_time(self):
        """delay_seconds=0 must not set schedule_time on the task."""
        from src.handlers.daily_notification import _enqueue_plan_task

        mock_client = MagicMock()
        mock_client.queue_path.return_value = "projects/p/locations/l/queues/q"
        mock_client.create_task.return_value = MagicMock()

        with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
            with patch("google.cloud.tasks_v2.HttpMethod") as mock_method:
                mock_method.POST = "POST"
                with patch("google.protobuf.timestamp_pb2.Timestamp"):
                    _enqueue_plan_task("uid1", 0)

        mock_client.create_task.assert_called_once()
        task_arg = mock_client.create_task.call_args[1]["task"]
        assert "schedule_time" not in task_arg

    def test_with_positive_delay_sets_schedule_time(self):
        """delay_seconds > 0 must set schedule_time on the task."""
        from src.handlers.daily_notification import _enqueue_plan_task

        mock_client = MagicMock()
        mock_client.queue_path.return_value = "projects/p/locations/l/queues/q"
        mock_client.create_task.return_value = MagicMock()
        mock_ts = MagicMock()

        with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
            with patch("google.cloud.tasks_v2.HttpMethod") as mock_method:
                mock_method.POST = "POST"
                with patch("google.protobuf.timestamp_pb2.Timestamp", return_value=mock_ts):
                    _enqueue_plan_task("uid1", 3600)

        mock_client.create_task.assert_called_once()
        task_arg = mock_client.create_task.call_args[1]["task"]
        assert "schedule_time" in task_arg
        assert task_arg["schedule_time"] is mock_ts
