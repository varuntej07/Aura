"""
Tests for src/services/daily_notification/orchestrator.py

Covers: _get_agents singleton, run_daily_plan error wrapping, _run pipeline
(plan exists, cap reached, happy path, retry path, safe-default path),
and all Firestore helper functions.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.daily_notification.models import DailyPlan, NudgePlan, VerificationResult


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _nudge(topic="nutrition", send_time="09:00"):
    return NudgePlan(
        topic=topic, title="T", body="B",
        send_at_local_time=send_time, send_at_utc="2026-05-03T09:00:00+00:00",
        why_this_topic="r", opening_chat_message="Hey", quick_reply_chips=["OK"],
    )


def _plan(morning="09:00", evening="19:00"):
    return DailyPlan(
        morning_nudge=_nudge("nutrition", morning),
        evening_nudge=_nudge("workout", evening),
        plan_source="query_based",
    )


def _approved():
    return VerificationResult(approved=True, rejection_reason=None, feedback_for_planner=None)


def _rejected(reason="bad", feedback="fix it"):
    return VerificationResult(approved=False, rejection_reason=reason, feedback_for_planner=feedback)


def _reset_agent_singletons():
    import src.services.daily_notification.orchestrator as orch
    orch._models = None
    orch._planner = None
    orch._verifier = None
    orch._suggestion_pills = None


# ---------------------------------------------------------------------------
# _get_agents
# ---------------------------------------------------------------------------

class TestGetAgents:
    def test_initialises_singletons_on_first_call(self):
        import src.services.daily_notification.orchestrator as orch
        _reset_agent_singletons()
        try:
            with patch("src.services.daily_notification.orchestrator.ModelProvider") as mock_mp:
                with patch("src.services.daily_notification.orchestrator.NotificationPlannerAgent") as mock_pa:
                    with patch("src.services.daily_notification.orchestrator.PushNotificationAgent") as mock_va:
                        with patch("src.services.daily_notification.orchestrator.SuggestionPillsAgent") as mock_sp:
                            planner, verifier, pills = orch._get_agents()
            mock_mp.assert_called_once()
            mock_pa.assert_called_once()
            mock_va.assert_called_once()
            mock_sp.assert_called_once()
        finally:
            _reset_agent_singletons()

    def test_returns_same_instance_on_second_call(self):
        import src.services.daily_notification.orchestrator as orch
        _reset_agent_singletons()
        try:
            with patch("src.services.daily_notification.orchestrator.ModelProvider"):
                with patch("src.services.daily_notification.orchestrator.NotificationPlannerAgent"):
                    with patch("src.services.daily_notification.orchestrator.PushNotificationAgent"):
                        with patch("src.services.daily_notification.orchestrator.SuggestionPillsAgent"):
                            p1, v1, s1 = orch._get_agents()
                            p2, v2, s2 = orch._get_agents()
            assert p1 is p2
            assert v1 is v2
            assert s1 is s2
        finally:
            _reset_agent_singletons()


# ---------------------------------------------------------------------------
# run_daily_plan
# ---------------------------------------------------------------------------

class TestRunDailyPlan:
    @pytest.mark.asyncio
    async def test_swallows_exception_from_run(self):
        """run_daily_plan must never raise even when _run fails."""
        from src.services.daily_notification.orchestrator import run_daily_plan

        with patch("src.services.daily_notification.orchestrator._run", new=AsyncMock(side_effect=RuntimeError("crash"))):
            await run_daily_plan("uid1")  # must not raise


# ---------------------------------------------------------------------------
# _run pipeline
# ---------------------------------------------------------------------------

def _patch_run(
    plan_exists=False,
    cap_reached=False,
    user_tz="UTC",
    queries=None,
    dietary=None,
    recent_plans=None,
    news=None,
    plan=None,
    verify_results=None,
    write_ok=True,
    schedule_ok=True,
):
    """Return a dict of patches needed for _run."""
    if plan is None:
        plan = _plan()
    if verify_results is None:
        verify_results = [_approved()]
    if news is None:
        news = [{"title": "News item"}]

    mock_planner = AsyncMock()
    mock_planner.generate = AsyncMock(return_value=plan)

    mock_verifier = AsyncMock()
    mock_verifier.verify = AsyncMock(side_effect=verify_results)

    mock_pills = AsyncMock()
    mock_pills.generate_all_agent_suggestion_pills = AsyncMock()

    return {
        "_daily_plan_exists": AsyncMock(return_value=plan_exists),
        "_load_user_timezone": AsyncMock(return_value=user_tz),
        "_daily_cap_reached": AsyncMock(return_value=cap_reached),
        "_fetch_last_10_queries": AsyncMock(return_value=queries or []),
        "_fetch_dietary_profile": AsyncMock(return_value=dietary or {}),
        "_fetch_last_2_daily_plans": AsyncMock(return_value=recent_plans or []),
        "rss_client.fetch_news": AsyncMock(return_value=news),
        "_get_agents": MagicMock(return_value=(mock_planner, mock_verifier, mock_pills)),
        "_write_daily_plan": AsyncMock(),
        "_schedule_nudge_send": AsyncMock(return_value=schedule_ok),
    }


def _apply_patches(patches: dict):
    """Return a context manager that applies all patches."""
    import contextlib
    base = "src.services.daily_notification.orchestrator"

    @contextlib.asynccontextmanager
    async def _ctx():
        with patch(f"{base}._daily_plan_exists", patches["_daily_plan_exists"]):
            with patch(f"{base}._load_user_timezone", patches["_load_user_timezone"]):
                with patch(f"{base}._daily_cap_reached", patches["_daily_cap_reached"]):
                    with patch(f"{base}._fetch_last_10_queries", patches["_fetch_last_10_queries"]):
                        with patch(f"{base}._fetch_dietary_profile", patches["_fetch_dietary_profile"]):
                            with patch(f"{base}._fetch_last_2_daily_plans", patches["_fetch_last_2_daily_plans"]):
                                with patch(f"{base}.rss_client.fetch_news", patches["rss_client.fetch_news"]):
                                    with patch(f"{base}._get_agents", patches["_get_agents"]):
                                        with patch(f"{base}._write_daily_plan", patches["_write_daily_plan"]):
                                            with patch(f"{base}._schedule_nudge_send", patches["_schedule_nudge_send"]):
                                                yield patches

    return _ctx()


class TestRunPipeline:
    @pytest.mark.asyncio
    async def test_skips_when_plan_already_exists(self):
        from src.services.daily_notification.orchestrator import _run
        patches = _patch_run(plan_exists=True)
        async with _apply_patches(patches):
            await _run("uid1")
        patches["_write_daily_plan"].assert_not_called()

    @pytest.mark.asyncio
    async def test_skips_when_daily_cap_reached(self):
        from src.services.daily_notification.orchestrator import _run
        patches = _patch_run(cap_reached=True)
        async with _apply_patches(patches):
            await _run("uid1")
        patches["_write_daily_plan"].assert_not_called()

    @pytest.mark.asyncio
    async def test_happy_path_writes_plan_and_schedules_tasks(self):
        from src.services.daily_notification.orchestrator import _run
        patches = _patch_run()
        async with _apply_patches(patches):
            await _run("uid1")
        patches["_write_daily_plan"].assert_called_once()
        assert patches["_schedule_nudge_send"].call_count == 2
        pills = patches["_get_agents"].return_value[2]
        pills.generate_all_agent_suggestion_pills.assert_called_once_with("uid1", [])

    @pytest.mark.asyncio
    async def test_retry_when_first_verify_fails(self):
        from src.services.daily_notification.orchestrator import _run
        patches = _patch_run(
            verify_results=[_rejected(), _approved()],
        )
        async with _apply_patches(patches):
            await _run("uid1")
        # Planner called twice (initial + retry)
        planner = patches["_get_agents"].return_value[0]
        assert planner.generate.call_count == 2
        patches["_write_daily_plan"].assert_called_once()

    @pytest.mark.asyncio
    async def test_safe_default_when_both_verifications_fail(self):
        from src.services.daily_notification.orchestrator import _run
        patches = _patch_run(
            verify_results=[_rejected("bad1"), _rejected("bad2")],
        )
        async with _apply_patches(patches):
            await _run("uid1")
        # Plan must still be written (with safe default)
        patches["_write_daily_plan"].assert_called_once()
        write_args = patches["_write_daily_plan"].call_args[0]
        written_plan: DailyPlan = write_args[2]
        assert written_plan.plan_source == "safe_default"

    @pytest.mark.asyncio
    async def test_logs_error_when_schedule_tasks_fail(self):
        from src.services.daily_notification.orchestrator import _run
        patches = _patch_run(schedule_ok=False)
        async with _apply_patches(patches):
            await _run("uid1")  # must not raise
        patches["_write_daily_plan"].assert_called_once()


# ---------------------------------------------------------------------------
# Firestore helpers
# ---------------------------------------------------------------------------

def _make_db_with_doc(exists: bool, data: dict | None = None):
    doc = MagicMock()
    doc.exists = exists
    doc.to_dict = MagicMock(return_value=data or {})
    chain = MagicMock()
    chain.get.return_value = doc
    db = MagicMock()
    db.collection.return_value.document.return_value.collection.return_value.document.return_value = chain
    return db, doc


class TestDailyPlanExists:
    @pytest.mark.asyncio
    async def test_returns_true_when_doc_exists(self):
        from src.services.daily_notification.orchestrator import _daily_plan_exists
        db, _ = _make_db_with_doc(exists=True)
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _daily_plan_exists("u1", "2026-05-03") is True

    @pytest.mark.asyncio
    async def test_returns_false_when_doc_missing(self):
        from src.services.daily_notification.orchestrator import _daily_plan_exists
        db, _ = _make_db_with_doc(exists=False)
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _daily_plan_exists("u1", "2026-05-03") is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        from src.services.daily_notification.orchestrator import _daily_plan_exists
        with patch("src.services.daily_notification.orchestrator.admin_firestore", side_effect=Exception):
            assert await _daily_plan_exists("u1", "2026-05-03") is False


class TestDailyCapReached:
    @pytest.mark.asyncio
    async def test_cap_reached_when_count_meets_max(self):
        from src.services.daily_notification.orchestrator import _daily_cap_reached
        from src.services.engagement.decision_engine import MAX_DAILY_PROACTIVE_NOTIFICATIONS

        today = datetime.now(timezone.utc).date().isoformat()
        data = {"guard_date": today, "proactive_notifications_sent_today": MAX_DAILY_PROACTIVE_NOTIFICATIONS}
        db, _ = _make_db_with_doc(exists=True, data=data)
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _daily_cap_reached("u1", today) is True

    @pytest.mark.asyncio
    async def test_cap_not_reached_when_count_below_max(self):
        from src.services.daily_notification.orchestrator import _daily_cap_reached

        today = datetime.now(timezone.utc).date().isoformat()
        data = {"guard_date": today, "proactive_notifications_sent_today": 0}
        db, _ = _make_db_with_doc(exists=True, data=data)
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _daily_cap_reached("u1", today) is False

    @pytest.mark.asyncio
    async def test_cap_not_reached_when_doc_missing(self):
        from src.services.daily_notification.orchestrator import _daily_cap_reached
        db, _ = _make_db_with_doc(exists=False)
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _daily_cap_reached("u1", "2026-05-03") is False

    @pytest.mark.asyncio
    async def test_different_guard_date_resets_count(self):
        from src.services.daily_notification.orchestrator import _daily_cap_reached
        from src.services.engagement.decision_engine import MAX_DAILY_PROACTIVE_NOTIFICATIONS
        # Guard doc has yesterday's date with a maxed count — today should not be capped
        data = {"guard_date": "2026-05-02", "proactive_notifications_sent_today": MAX_DAILY_PROACTIVE_NOTIFICATIONS}
        db, _ = _make_db_with_doc(exists=True, data=data)
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _daily_cap_reached("u1", "2026-05-03") is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        from src.services.daily_notification.orchestrator import _daily_cap_reached
        with patch("src.services.daily_notification.orchestrator.admin_firestore", side_effect=Exception):
            assert await _daily_cap_reached("u1", "2026-05-03") is False


class TestLoadUserTimezone:
    @pytest.mark.asyncio
    async def test_returns_timezone_from_doc(self):
        from src.services.daily_notification.orchestrator import _load_user_timezone
        doc = MagicMock()
        doc.exists = True
        doc.to_dict = MagicMock(return_value={"timezone": "Asia/Kolkata"})
        db = MagicMock()
        db.collection.return_value.document.return_value.get.return_value = doc
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _load_user_timezone("u1") == "Asia/Kolkata"

    @pytest.mark.asyncio
    async def test_returns_utc_when_doc_missing(self):
        from src.services.daily_notification.orchestrator import _load_user_timezone
        doc = MagicMock()
        doc.exists = False
        db = MagicMock()
        db.collection.return_value.document.return_value.get.return_value = doc
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _load_user_timezone("u1") == "UTC"

    @pytest.mark.asyncio
    async def test_returns_utc_on_exception(self):
        from src.services.daily_notification.orchestrator import _load_user_timezone
        with patch("src.services.daily_notification.orchestrator.admin_firestore", side_effect=Exception):
            assert await _load_user_timezone("u1") == "UTC"


class TestFetchLastQueries:
    @pytest.mark.asyncio
    async def test_returns_list_of_dicts(self):
        from src.services.daily_notification.orchestrator import _fetch_last_10_queries
        doc = MagicMock()
        doc.id = "q1"
        doc.to_dict = MagicMock(return_value={"text": "hello", "timestamp": "2026-05-03"})
        chain = MagicMock()
        chain.order_by.return_value.limit.return_value.stream.return_value = [doc]
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value = chain
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            result = await _fetch_last_10_queries("u1")
        assert len(result) == 1
        assert result[0]["text"] == "hello"

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self):
        from src.services.daily_notification.orchestrator import _fetch_last_10_queries
        with patch("src.services.daily_notification.orchestrator.admin_firestore", side_effect=Exception):
            assert await _fetch_last_10_queries("u1") == []


class TestFetchDietaryProfile:
    @pytest.mark.asyncio
    async def test_returns_profile_dict(self):
        from src.services.daily_notification.orchestrator import _fetch_dietary_profile
        db, _ = _make_db_with_doc(exists=True, data={"goal": "lose weight"})
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            result = await _fetch_dietary_profile("u1")
        assert result["goal"] == "lose weight"

    @pytest.mark.asyncio
    async def test_returns_empty_when_doc_missing(self):
        from src.services.daily_notification.orchestrator import _fetch_dietary_profile
        db, _ = _make_db_with_doc(exists=False)
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            assert await _fetch_dietary_profile("u1") == {}

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self):
        from src.services.daily_notification.orchestrator import _fetch_dietary_profile
        with patch("src.services.daily_notification.orchestrator.admin_firestore", side_effect=Exception):
            assert await _fetch_dietary_profile("u1") == {}


class TestFetchLastDailyPlans:
    @pytest.mark.asyncio
    async def test_returns_list_of_plan_dicts(self):
        from src.services.daily_notification.orchestrator import _fetch_last_2_daily_plans
        doc = MagicMock()
        doc.to_dict = MagicMock(return_value={"plan_date": "2026-05-02"})
        chain = MagicMock()
        chain.order_by.return_value.limit.return_value.stream.return_value = [doc]
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value = chain
        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            result = await _fetch_last_2_daily_plans("u1")
        assert len(result) == 1
        assert result[0]["plan_date"] == "2026-05-02"

    @pytest.mark.asyncio
    async def test_returns_empty_on_exception(self):
        from src.services.daily_notification.orchestrator import _fetch_last_2_daily_plans
        with patch("src.services.daily_notification.orchestrator.admin_firestore", side_effect=Exception):
            assert await _fetch_last_2_daily_plans("u1") == []


class TestWriteDailyPlan:
    @pytest.mark.asyncio
    async def test_calls_firestore_set(self):
        from src.services.daily_notification.orchestrator import _write_daily_plan

        plan_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = plan_ref

        with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
            await _write_daily_plan("u1", "2026-05-03", _plan(), 0, None)

        plan_ref.set.assert_called_once()
        doc = plan_ref.set.call_args[0][0]
        assert doc["plan_date"] == "2026-05-03"
        assert "morning_nudge" in doc
        assert "evening_nudge" in doc

    @pytest.mark.asyncio
    async def test_raises_on_exception(self):
        from src.services.daily_notification.orchestrator import _write_daily_plan

        with patch("src.services.daily_notification.orchestrator.admin_firestore", side_effect=Exception("db down")):
            with pytest.raises(Exception, match="db down"):
                await _write_daily_plan("u1", "2026-05-03", _plan(), 0, None)


class TestScheduleNudgeSend:
    @pytest.mark.asyncio
    async def test_creates_cloud_task_and_returns_true(self):
        from src.services.daily_notification.orchestrator import _schedule_nudge_send

        mock_client = MagicMock()
        created_task = MagicMock()
        created_task.name = "projects/p/locations/l/queues/q/tasks/t1"
        mock_client.queue_path.return_value = "projects/p/locations/l/queues/q"
        mock_client.create_task.return_value = created_task

        plan_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = plan_ref

        with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
            with patch("google.cloud.tasks_v2.HttpMethod") as mock_method:
                mock_method.POST = "POST"
                with patch("google.protobuf.timestamp_pb2.Timestamp"):
                    with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
                        result = await _schedule_nudge_send(
                            "u1", "2026-05-03", "morning_nudge",
                            "2026-05-03T09:00:00+00:00",
                        )

        assert result is True
        mock_client.create_task.assert_called_once()

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        from src.services.daily_notification.orchestrator import _schedule_nudge_send

        with patch("google.cloud.tasks_v2.CloudTasksClient", side_effect=Exception("tasks down")):
            result = await _schedule_nudge_send("u1", "2026-05-03", "morning_nudge", "2026-05-03T09:00:00+00:00")

        assert result is False

    @pytest.mark.asyncio
    async def test_malformed_utc_falls_back_to_1h_from_now(self):
        """If send_at_utc can't be parsed, Cloud Task is still created with +1h fallback."""
        from src.services.daily_notification.orchestrator import _schedule_nudge_send

        mock_client = MagicMock()
        mock_client.queue_path.return_value = "projects/p/locations/l/queues/q"
        mock_client.create_task.return_value = MagicMock(name="task_name")

        plan_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = plan_ref

        with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
            with patch("google.cloud.tasks_v2.HttpMethod") as mock_method:
                mock_method.POST = "POST"
                with patch("google.protobuf.timestamp_pb2.Timestamp"):
                    with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
                        result = await _schedule_nudge_send(
                            "u1", "2026-05-03", "morning_nudge", "not-a-datetime",
                        )

        assert result is True

    @pytest.mark.asyncio
    async def test_naive_utc_string_gets_utc_timezone(self):
        """A naive ISO datetime (no tzinfo) must have UTC attached, not fall back to +1h."""
        from src.services.daily_notification.orchestrator import _schedule_nudge_send

        mock_client = MagicMock()
        mock_client.queue_path.return_value = "projects/p/locations/l/queues/q"
        created_task = MagicMock()
        created_task.name = "projects/p/locations/l/queues/q/tasks/t2"
        mock_client.create_task.return_value = created_task

        plan_ref = MagicMock()
        db = MagicMock()
        db.collection.return_value.document.return_value.collection.return_value.document.return_value = plan_ref

        with patch("google.cloud.tasks_v2.CloudTasksClient", return_value=mock_client):
            with patch("google.cloud.tasks_v2.HttpMethod") as mock_method:
                mock_method.POST = "POST"
                with patch("google.protobuf.timestamp_pb2.Timestamp"):
                    with patch("src.services.daily_notification.orchestrator.admin_firestore", return_value=db):
                        result = await _schedule_nudge_send(
                            "u1", "2026-05-03", "morning_nudge",
                            "2026-05-03T09:00:00",  # naive — no +00:00
                        )

        assert result is True
        mock_client.create_task.assert_called_once()
