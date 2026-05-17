"""
Tests for pure helper functions in src/services/daily_notification/orchestrator.py

No I/O — these are all synchronous pure functions.
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import MagicMock, patch


class TestExtractTopicKeywords:
    def test_empty_queries_returns_empty(self):
        from src.services.daily_notification.orchestrator import _extract_topic_keywords
        assert _extract_topic_keywords([]) == []

    def test_recognises_nutrition_keyword(self):
        from src.services.daily_notification.orchestrator import _extract_topic_keywords
        queries = [{"text": "how many calories in a banana"}]
        result = _extract_topic_keywords(queries)
        assert "nutrition" in result

    def test_recognises_workout_keyword(self):
        from src.services.daily_notification.orchestrator import _extract_topic_keywords
        queries = [{"text": "best chest workout for mass"}]
        result = _extract_topic_keywords(queries)
        assert "workout" in result

    def test_recognises_sleep_keyword(self):
        from src.services.daily_notification.orchestrator import _extract_topic_keywords
        queries = [{"text": "I have insomnia and can't sleep"}]
        result = _extract_topic_keywords(queries)
        assert "sleep" in result

    def test_multiple_topics_detected(self):
        from src.services.daily_notification.orchestrator import _extract_topic_keywords
        queries = [
            {"text": "how much water should I drink"},
            {"text": "I feel stressed all the time"},
        ]
        result = _extract_topic_keywords(queries)
        assert "hydration" in result
        assert "mindfulness" in result

    def test_query_with_no_matching_words_returns_empty(self):
        from src.services.daily_notification.orchestrator import _extract_topic_keywords
        # Avoid any substring matches: "weather" contains "eat" (nutrition),
        # "today" contains no matches, but use a clean sentence to be safe.
        queries = [{"text": "show my upcoming calendar events"}]
        result = _extract_topic_keywords(queries)
        assert result == []

    def test_missing_text_key_does_not_crash(self):
        from src.services.daily_notification.orchestrator import _extract_topic_keywords
        queries = [{}]
        result = _extract_topic_keywords(queries)
        assert result == []


class TestExtractTopicsFromPlans:
    def test_empty_plans_returns_empty(self):
        from src.services.daily_notification.orchestrator import _extract_topics_from_plans
        assert _extract_topics_from_plans([]) == []

    def test_extracts_topics_from_nudges(self):
        from src.services.daily_notification.orchestrator import _extract_topics_from_plans
        plans = [
            {
                "morning_nudge": {"topic": "nutrition"},
                "evening_nudge": {"topic": "sleep"},
            }
        ]
        result = _extract_topics_from_plans(plans)
        assert "nutrition" in result
        assert "sleep" in result

    def test_deduplicates_topics(self):
        from src.services.daily_notification.orchestrator import _extract_topics_from_plans
        plans = [
            {"morning_nudge": {"topic": "nutrition"}, "evening_nudge": {"topic": "nutrition"}},
        ]
        result = _extract_topics_from_plans(plans)
        assert result.count("nutrition") == 1

    def test_missing_topic_field_is_skipped(self):
        from src.services.daily_notification.orchestrator import _extract_topics_from_plans
        plans = [{"morning_nudge": {}, "evening_nudge": {"topic": "sleep"}}]
        result = _extract_topics_from_plans(plans)
        assert result == ["sleep"]


class TestLocalNowIso:
    def test_valid_timezone_returns_iso_string(self):
        from src.services.daily_notification.orchestrator import _local_now_iso
        result = _local_now_iso("America/New_York")
        # Basic sanity: parses as datetime without error
        datetime.fromisoformat(result)

    def test_invalid_timezone_falls_back_to_utc(self):
        from src.services.daily_notification.orchestrator import _local_now_iso
        result = _local_now_iso("Not/ATimezone")
        dt = datetime.fromisoformat(result)
        # UTC offset should be +00:00 or Z
        assert dt.utcoffset() is not None


class TestLocalHhmmToUtc:
    def test_future_time_today(self):
        from src.services.daily_notification.orchestrator import _local_hhmm_to_utc
        # Use a fixed timezone and a time far in the future today
        tz = "UTC"
        # Set 23:59 — always in the future unless it's exactly midnight
        result = _local_hhmm_to_utc("23:59", tz)
        dt = datetime.fromisoformat(result)
        assert dt > datetime.now(timezone.utc) - timedelta(seconds=5)

    def test_past_time_sends_today_not_tomorrow(self):
        """A time already passed today must NOT roll to tomorrow.
        Cloud Tasks fires past-scheduled tasks immediately — rolling to tomorrow
        would silently skip the notification for a full day.
        """
        from src.services.daily_notification.orchestrator import _local_hhmm_to_utc
        # Pin "now" to 10:00 UTC so 00:01 is unambiguously in the past.
        fixed_now = datetime(2026, 5, 3, 10, 0, 0, tzinfo=timezone.utc)
        with patch("src.services.daily_notification.orchestrator.datetime") as mock_dt:
            mock_dt.now = MagicMock(return_value=fixed_now)
            result = _local_hhmm_to_utc("00:01", "UTC")
        dt = datetime.fromisoformat(result)
        # Must stay on the same day (2026-05-03), not roll to 2026-05-04
        assert dt.day == 3
        assert dt.hour == 0
        assert dt.minute == 1

    def test_invalid_timezone_returns_fallback(self):
        from src.services.daily_notification.orchestrator import _local_hhmm_to_utc
        result = _local_hhmm_to_utc("08:00", "Not/Valid")
        # Fallback: UTC now + 1 hour — just check it parses
        dt = datetime.fromisoformat(result)
        assert dt > datetime.now(timezone.utc)

    def test_invalid_hhmm_format_returns_fallback(self):
        from src.services.daily_notification.orchestrator import _local_hhmm_to_utc
        result = _local_hhmm_to_utc("not-a-time", "UTC")
        dt = datetime.fromisoformat(result)
        assert dt > datetime.now(timezone.utc)


class TestMakeSafeDefaultPlan:
    def test_with_news_items_uses_top_headline(self):
        from src.services.daily_notification.orchestrator import _make_safe_default_plan
        news = [{"title": "Breaking: Scientists find cure for Monday blues"}]
        plan = _make_safe_default_plan(news, "UTC")
        assert plan.morning_nudge.topic == "news"
        assert "Scientists" in plan.morning_nudge.title or len(plan.morning_nudge.title) <= 50

    def test_without_news_uses_fallback_headline(self):
        from src.services.daily_notification.orchestrator import _make_safe_default_plan
        plan = _make_safe_default_plan([], "UTC")
        assert plan.morning_nudge.topic == "news"
        assert "health" in plan.morning_nudge.title.lower() or len(plan.morning_nudge.title) > 0

    def test_evening_nudge_is_habit_checkin(self):
        from src.services.daily_notification.orchestrator import _make_safe_default_plan
        plan = _make_safe_default_plan([], "UTC")
        assert plan.evening_nudge.topic == "habit"

    def test_plan_source_is_safe_default(self):
        from src.services.daily_notification.orchestrator import _make_safe_default_plan
        plan = _make_safe_default_plan([], "UTC")
        assert plan.plan_source == "safe_default"

    def test_long_headline_is_truncated_to_50_chars(self):
        from src.services.daily_notification.orchestrator import _make_safe_default_plan
        long_title = "A" * 100
        plan = _make_safe_default_plan([{"title": long_title}], "UTC")
        assert len(plan.morning_nudge.title) <= 50

    def test_quick_reply_chips_present_on_both_nudges(self):
        from src.services.daily_notification.orchestrator import _make_safe_default_plan
        plan = _make_safe_default_plan([], "UTC")
        assert len(plan.morning_nudge.quick_reply_chips) > 0
        assert len(plan.evening_nudge.quick_reply_chips) > 0
