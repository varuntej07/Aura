"""
Tests for src/services/daily_notification/planner_agent.py

Covers: _build_prompt, _summarise_queries, _summarise_news,
NotificationPlannerAgent.generate.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.services.daily_notification.models import DailyPlan, NudgePlan


def _make_plan():
    nudge = NudgePlan(
        topic="nutrition", title="T", body="B",
        send_at_local_time="09:00", send_at_utc="2026-05-03T09:00:00+00:00",
        why_this_topic="test", opening_chat_message="Hey",
        quick_reply_chips=["OK"],
    )
    return DailyPlan(
        morning_nudge=nudge,
        evening_nudge=nudge.model_copy(update={"topic": "workout", "send_at_local_time": "19:00"}),
        plan_source="query_based",
    )


class TestSummariseQueries:
    def test_empty_returns_no_recent_queries(self):
        from src.services.daily_notification.planner_agent import _summarise_queries
        assert _summarise_queries([]) == "No recent queries."

    def test_formats_query_with_timestamp(self):
        from src.services.daily_notification.planner_agent import _summarise_queries
        queries = [{"text": "how much protein", "type": "chat", "timestamp": "2026-05-03T08:00:00Z"}]
        result = _summarise_queries(queries)
        assert "how much protein" in result
        assert "2026-05-03" in result
        assert "[chat]" in result

    def test_skips_empty_text(self):
        from src.services.daily_notification.planner_agent import _summarise_queries
        queries = [{"text": "", "type": "chat", "timestamp": "2026-05-03"}]
        result = _summarise_queries(queries)
        assert result == "No recent queries."

    def test_missing_timestamp_shows_unknown_date(self):
        from src.services.daily_notification.planner_agent import _summarise_queries
        queries = [{"text": "test query", "type": "voice"}]
        result = _summarise_queries(queries)
        assert "unknown date" in result

    def test_caps_at_10_queries(self):
        from src.services.daily_notification.planner_agent import _summarise_queries
        queries = [{"text": f"query {i}", "type": "chat", "timestamp": "2026-05-03"} for i in range(15)]
        result = _summarise_queries(queries)
        assert result.count("[chat]") == 10


class TestSummariseNews:
    def test_empty_returns_no_news(self):
        from src.services.daily_notification.planner_agent import _summarise_news
        assert _summarise_news([]) == "No news available."

    def test_formats_title_and_date(self):
        from src.services.daily_notification.planner_agent import _summarise_news
        items = [{"title": "Big study", "summary": "Details here", "published_at": "May 03, 2026"}]
        result = _summarise_news(items)
        assert "Big study" in result
        assert "May 03, 2026" in result

    def test_truncates_long_summary(self):
        from src.services.daily_notification.planner_agent import _summarise_news
        items = [{"title": "T", "summary": "S" * 200, "published_at": ""}]
        result = _summarise_news(items)
        assert "..." in result

    def test_short_summary_not_truncated(self):
        from src.services.daily_notification.planner_agent import _summarise_news
        items = [{"title": "T", "summary": "Short", "published_at": ""}]
        result = _summarise_news(items)
        assert "Short" in result
        assert "..." not in result

    def test_caps_at_5_items(self):
        from src.services.daily_notification.planner_agent import _summarise_news
        items = [{"title": f"Story {i}", "summary": "", "published_at": ""} for i in range(8)]
        result = _summarise_news(items)
        assert result.count("•") == 5

    def test_skips_item_without_title(self):
        from src.services.daily_notification.planner_agent import _summarise_news
        items = [{"title": "", "summary": "Orphan summary", "published_at": ""}]
        result = _summarise_news(items)
        assert result == "No news available."


class TestBuildPrompt:
    def test_includes_timezone_and_datetime(self):
        from src.services.daily_notification.planner_agent import _build_prompt
        ctx = {
            "recent_queries": [],
            "dietary_profile": {},
            "topics_sent_yesterday": [],
            "news_items": [],
            "user_timezone": "America/New_York",
            "current_local_datetime": "2026-05-03T07:00:00-04:00",
            "retry_feedback": None,
        }
        prompt = _build_prompt(ctx)
        assert "America/New_York" in prompt
        assert "2026-05-03" in prompt

    def test_includes_dietary_profile_fields(self):
        from src.services.daily_notification.planner_agent import _build_prompt
        ctx = {
            "recent_queries": [],
            "dietary_profile": {"goal": "build muscle", "activity_level": "high"},
            "topics_sent_yesterday": [],
            "news_items": [],
            "user_timezone": "UTC",
            "current_local_datetime": "",
            "retry_feedback": None,
        }
        prompt = _build_prompt(ctx)
        assert "build muscle" in prompt
        assert "high" in prompt

    def test_includes_restrictions_and_allergies(self):
        from src.services.daily_notification.planner_agent import _build_prompt
        ctx = {
            "recent_queries": [],
            "dietary_profile": {
                "restrictions": ["vegan", "gluten-free"],
                "allergies": ["peanuts"],
            },
            "topics_sent_yesterday": [],
            "news_items": [],
            "user_timezone": "UTC",
            "current_local_datetime": "",
            "retry_feedback": None,
        }
        prompt = _build_prompt(ctx)
        assert "vegan" in prompt
        assert "peanuts" in prompt

    def test_includes_topics_sent_yesterday(self):
        from src.services.daily_notification.planner_agent import _build_prompt
        ctx = {
            "recent_queries": [],
            "dietary_profile": {},
            "topics_sent_yesterday": ["nutrition", "sleep"],
            "news_items": [],
            "user_timezone": "UTC",
            "current_local_datetime": "",
            "retry_feedback": None,
        }
        prompt = _build_prompt(ctx)
        assert "nutrition" in prompt
        assert "sleep" in prompt

    def test_no_topics_yesterday_says_none(self):
        from src.services.daily_notification.planner_agent import _build_prompt
        ctx = {
            "recent_queries": [],
            "dietary_profile": {},
            "topics_sent_yesterday": [],
            "news_items": [],
            "user_timezone": "UTC",
            "current_local_datetime": "",
            "retry_feedback": None,
        }
        prompt = _build_prompt(ctx)
        assert "No notifications sent in the last 2 days" in prompt

    def test_retry_feedback_appended(self):
        from src.services.daily_notification.planner_agent import _build_prompt
        ctx = {
            "recent_queries": [],
            "dietary_profile": {},
            "topics_sent_yesterday": [],
            "news_items": [],
            "user_timezone": "UTC",
            "current_local_datetime": "",
            "retry_feedback": "Fix the morning time",
        }
        prompt = _build_prompt(ctx)
        assert "Fix the morning time" in prompt
        assert "REJECTED" in prompt

    def test_no_retry_feedback_not_appended(self):
        from src.services.daily_notification.planner_agent import _build_prompt
        ctx = {
            "recent_queries": [],
            "dietary_profile": {},
            "topics_sent_yesterday": [],
            "news_items": [],
            "user_timezone": "UTC",
            "current_local_datetime": "",
            "retry_feedback": None,
        }
        prompt = _build_prompt(ctx)
        assert "REJECTED" not in prompt


class TestNotificationPlannerAgentGenerate:
    @pytest.mark.asyncio
    async def test_generate_calls_model_and_returns_plan(self):
        from src.services.daily_notification.planner_agent import NotificationPlannerAgent

        expected_plan = _make_plan()
        mock_models = MagicMock()
        mock_models.cheap = AsyncMock(return_value=expected_plan)

        agent = NotificationPlannerAgent(mock_models)
        ctx = {
            "recent_queries": [],
            "dietary_profile": {},
            "topics_sent_yesterday": [],
            "news_items": [],
            "user_timezone": "UTC",
            "current_local_datetime": "2026-05-03T07:00:00+00:00",
            "retry_feedback": None,
        }
        plan = await agent.generate(ctx)

        assert plan is expected_plan
        mock_models.cheap.assert_called_once()
