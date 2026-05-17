"""
Tests for src/services/daily_notification/verifier_agent.py

Covers: all Stage-1 hard rules, _parse_local_time, _summarise_profile,
PushNotificationAgent.verify (Stage 2 LLM path).
"""

from __future__ import annotations

from datetime import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.services.daily_notification.models import DailyPlan, NudgePlan, VerificationResult


def _nudge(topic="nutrition", send_time="09:00"):
    return NudgePlan(
        topic=topic,
        title="Test",
        body="Test body",
        send_at_local_time=send_time,
        send_at_utc="2026-05-03T09:00:00+00:00",
        why_this_topic="test",
        opening_chat_message="Hey",
        quick_reply_chips=["OK"],
    )


def _plan(morning_time="09:00", evening_time="18:00", morning_topic="nutrition", evening_topic="workout"):
    return DailyPlan(
        morning_nudge=_nudge(topic=morning_topic, send_time=morning_time),
        evening_nudge=_nudge(topic=evening_topic, send_time=evening_time),
        plan_source="query_based",
    )


class TestParseLocalTime:
    def test_valid_hhmm(self):
        from src.services.daily_notification.verifier_agent import _parse_local_time
        from datetime import time
        assert _parse_local_time("09:30") == time(9, 30)

    def test_midnight(self):
        from src.services.daily_notification.verifier_agent import _parse_local_time
        from datetime import time
        assert _parse_local_time("00:00") == time(0, 0)

    def test_invalid_string_returns_none(self):
        from src.services.daily_notification.verifier_agent import _parse_local_time
        assert _parse_local_time("not-a-time") is None

    def test_empty_string_returns_none(self):
        from src.services.daily_notification.verifier_agent import _parse_local_time
        assert _parse_local_time("") is None

    def test_single_segment_returns_none(self):
        from src.services.daily_notification.verifier_agent import _parse_local_time
        assert _parse_local_time("0930") is None

    def test_non_numeric_parts_returns_none(self):
        from src.services.daily_notification.verifier_agent import _parse_local_time
        # "aa:bb" has 2 segments but int("aa") raises ValueError → hits except branch
        assert _parse_local_time("aa:bb") is None


class TestSummariseProfile:
    def test_empty_profile(self):
        from src.services.daily_notification.verifier_agent import _summarise_profile
        result = _summarise_profile({})
        assert "No dietary profile" in result

    def test_with_fields(self):
        from src.services.daily_notification.verifier_agent import _summarise_profile
        result = _summarise_profile({"goal": "lose weight", "activity_level": "moderate"})
        assert "goal" in result
        assert "lose weight" in result
        assert "activity_level" in result

    def test_none_values_skipped(self):
        from src.services.daily_notification.verifier_agent import _summarise_profile
        result = _summarise_profile({"goal": None, "age": None})
        assert "No dietary profile" in result


class TestCheckHardRules:
    def test_valid_plan_returns_none(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        assert _check_hard_rules(_plan(), []) is None

    def test_morning_too_early_rejected(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        result = _check_hard_rules(_plan(morning_time="07:00"), [])
        assert result is not None
        assert result.approved is False
        assert "08:00" in result.rejection_reason

    def test_morning_too_late_rejected(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        result = _check_hard_rules(_plan(morning_time="13:00"), [])
        assert result is not None
        assert result.approved is False

    def test_morning_invalid_format_rejected(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        result = _check_hard_rules(_plan(morning_time="bad"), [])
        assert result is not None
        assert result.approved is False

    def test_evening_too_early_rejected(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        result = _check_hard_rules(_plan(evening_time="16:00"), [])
        assert result is not None
        assert result.approved is False
        assert "17:00" in result.rejection_reason

    def test_evening_too_late_rejected(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        result = _check_hard_rules(_plan(evening_time="22:00"), [])
        assert result is not None
        assert result.approved is False

    def test_gap_too_small_rejected(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        import src.services.daily_notification.verifier_agent as va
        # Expand both windows so 11:00 and 12:00 are valid window-wise (gap = 1h < 4h)
        with patch.object(va, "MORNING_WINDOW_END", time(14, 0)), \
             patch.object(va, "EVENING_WINDOW_START", time(9, 0)):
            result = _check_hard_rules(_plan(morning_time="11:00", evening_time="12:00"), [])
        assert result is not None
        assert "gap" in result.rejection_reason.lower()

    def test_same_topic_rejected(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        result = _check_hard_rules(_plan(morning_topic="nutrition", evening_topic="nutrition"), [])
        assert result is not None
        assert "nutrition" in result.rejection_reason

    def test_both_topics_repeated_from_yesterday_rejected(self):
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        result = _check_hard_rules(
            _plan(morning_topic="nutrition", evening_topic="workout"),
            topics_sent_yesterday=["nutrition", "workout"],
        )
        assert result is not None
        assert result.approved is False

    def test_one_topic_repeated_is_ok(self):
        """Only one topic repeated → still acceptable (one fresh topic present)."""
        from src.services.daily_notification.verifier_agent import _check_hard_rules
        result = _check_hard_rules(
            _plan(morning_topic="nutrition", evening_topic="sleep"),
            topics_sent_yesterday=["nutrition"],
        )
        assert result is None


class TestPushNotificationAgentVerify:
    @pytest.mark.asyncio
    async def test_stage1_failure_skips_llm(self):
        """Stage 1 rejection must not call the LLM."""
        from src.services.daily_notification.verifier_agent import PushNotificationAgent

        mock_models = MagicMock()
        agent = PushNotificationAgent(mock_models)

        bad_plan = _plan(morning_time="03:00")
        result = await agent.verify(bad_plan, [], {})

        assert result.approved is False
        mock_models.fast.assert_not_called()

    @pytest.mark.asyncio
    async def test_stage2_llm_called_on_valid_stage1(self):
        """Stage 2 LLM check must be called when Stage 1 passes."""
        from src.services.daily_notification.verifier_agent import PushNotificationAgent

        llm_result = VerificationResult(approved=True, rejection_reason=None, feedback_for_planner=None)
        mock_models = MagicMock()
        mock_models.cheap = AsyncMock(return_value=llm_result)

        agent = PushNotificationAgent(mock_models)
        result = await agent.verify(_plan(), [], {})

        assert result.approved is True
        mock_models.cheap.assert_called_once()

    @pytest.mark.asyncio
    async def test_stage2_rejection_propagated(self):
        from src.services.daily_notification.verifier_agent import PushNotificationAgent

        llm_result = VerificationResult(
            approved=False,
            rejection_reason="tone is condescending",
            feedback_for_planner="rewrite evening_nudge body",
        )
        mock_models = MagicMock()
        mock_models.cheap = AsyncMock(return_value=llm_result)

        agent = PushNotificationAgent(mock_models)
        result = await agent.verify(_plan(), [], {})

        assert result.approved is False
        assert "condescending" in result.rejection_reason
