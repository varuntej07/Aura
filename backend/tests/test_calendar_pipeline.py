"""
Tests for the calendar reminder pipeline in
src/services/daily_notification/orchestrator.py

Covers the two pure functions (no I/O):
  - _partition_events_for_notification_planning
  - _build_meeting_reminder_plans
"""

from datetime import UTC, datetime, timedelta
from typing import Literal

from src.services.daily_notification.models import CalendarNotificationContent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _event(event_id: str, start_iso: str) -> dict:
    return {"id": event_id, "title": "Meeting", "start_at": start_iso}


def _content(
    event_id: str,
    notification_type: Literal["three_hour_before", "three_day_ahead"] = "three_hour_before",
    importance_tier: Literal["high", "medium"] = "high",
) -> CalendarNotificationContent:
    return CalendarNotificationContent(
        event_id=event_id,
        event_title="Sprint Review",
        importance_tier=importance_tier,
        notification_type=notification_type,
        title="Sprint Review in 3 hours",
        body="Time to prep your demo.",
        opening_chat_message="Your sprint review is coming up.",
        quick_reply_chips=["Got it", "Remind me later"],
        why_this_notification="High-importance recurring meeting.",
    )


# ---------------------------------------------------------------------------
# _partition_events_for_notification_planning
# ---------------------------------------------------------------------------

class TestPartitionEventsForNotificationPlanning:
    def test_event_today_goes_into_today_bucket(self):
        from src.services.daily_notification.orchestrator import (
            _partition_events_for_notification_planning,
        )
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")
        now_utc = datetime.now(tz)
        start = now_utc.replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
        event = _event("e1", start)

        today, three_days = _partition_events_for_notification_planning([event], "UTC")

        assert len(today) == 1
        assert today[0]["id"] == "e1"
        assert len(three_days) == 0

    def test_event_three_days_out_goes_into_three_day_bucket(self):
        from src.services.daily_notification.orchestrator import (
            _partition_events_for_notification_planning,
        )
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")
        future = (datetime.now(tz) + timedelta(days=3)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        event = _event("e2", future.isoformat())

        today, three_days = _partition_events_for_notification_planning([event], "UTC")

        assert len(today) == 0
        assert len(three_days) == 1
        assert three_days[0]["id"] == "e2"

    def test_event_two_days_out_ignored(self):
        from src.services.daily_notification.orchestrator import (
            _partition_events_for_notification_planning,
        )
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")
        future = (datetime.now(tz) + timedelta(days=2)).replace(
            hour=10, minute=0, second=0, microsecond=0
        )
        event = _event("e3", future.isoformat())

        today, three_days = _partition_events_for_notification_planning([event], "UTC")

        assert len(today) == 0
        assert len(three_days) == 0

    def test_event_missing_start_at_skipped(self):
        from src.services.daily_notification.orchestrator import (
            _partition_events_for_notification_planning,
        )
        event = {"id": "e4", "title": "No time"}  # no start_at

        today, three_days = _partition_events_for_notification_planning([event], "UTC")

        assert today == []
        assert three_days == []

    def test_invalid_timezone_falls_back_to_utc(self):
        from src.services.daily_notification.orchestrator import (
            _partition_events_for_notification_planning,
        )
        now_utc = datetime.now(UTC)
        start = now_utc.replace(hour=14, minute=0, second=0, microsecond=0).isoformat()
        event = _event("e5", start)

        today, _ = _partition_events_for_notification_planning([event], "Not/A/Timezone")

        assert len(today) == 1

    def test_event_with_z_suffix_parsed_correctly(self):
        from src.services.daily_notification.orchestrator import (
            _partition_events_for_notification_planning,
        )
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("UTC")
        now_utc = datetime.now(tz)
        start = now_utc.replace(hour=15, minute=0, second=0, microsecond=0)
        start_z = start.strftime("%Y-%m-%dT%H:%M:%SZ")
        event = _event("e6", start_z)

        today, _ = _partition_events_for_notification_planning([event], "UTC")

        assert len(today) == 1


# ---------------------------------------------------------------------------
# _build_meeting_reminder_plans
# ---------------------------------------------------------------------------

class TestBuildMeetingReminderPlans:
    def _now(self) -> datetime:
        return datetime(2026, 6, 1, 7, 0, 0, tzinfo=UTC)

    def _event_start(self, hours_from_now: float) -> str:
        t = self._now() + timedelta(hours=hours_from_now)
        return t.isoformat()

    def test_three_hour_before_scheduled_correctly(self):
        from src.services.daily_notification.orchestrator import (
            _build_meeting_reminder_plans,
        )
        now = self._now()
        event_start = now + timedelta(hours=4)
        event = _event("ev1", event_start.isoformat())
        content = _content("ev1", notification_type="three_hour_before")

        plans = _build_meeting_reminder_plans([content], [event], now, "UTC")

        assert len(plans) == 1
        send_dt = datetime.fromisoformat(plans[0].send_at_utc)
        expected_send = event_start - timedelta(hours=3)
        assert abs((send_dt - expected_send).total_seconds()) < 5

    def test_three_hour_before_skipped_when_window_already_passed(self):
        from src.services.daily_notification.orchestrator import (
            _build_meeting_reminder_plans,
        )
        now = self._now()
        event_start = now + timedelta(hours=2)  # only 2h away, reminder window is past
        event = _event("ev2", event_start.isoformat())
        content = _content("ev2", notification_type="three_hour_before")

        plans = _build_meeting_reminder_plans([content], [event], now, "UTC")

        assert plans == []

    def test_three_day_ahead_sends_three_hours_from_now(self):
        from src.services.daily_notification.orchestrator import (
            _build_meeting_reminder_plans,
        )
        now = self._now()
        future_event = now + timedelta(days=3)
        event = _event("ev3", future_event.isoformat())
        content = _content("ev3", notification_type="three_day_ahead")

        plans = _build_meeting_reminder_plans([content], [event], now, "UTC")

        assert len(plans) == 1
        send_dt = datetime.fromisoformat(plans[0].send_at_utc)
        expected = now + timedelta(hours=3)
        assert abs((send_dt - expected).total_seconds()) < 5

    def test_unknown_event_id_skipped(self):
        from src.services.daily_notification.orchestrator import (
            _build_meeting_reminder_plans,
        )
        now = self._now()
        content = _content("no_match", notification_type="three_hour_before")

        plans = _build_meeting_reminder_plans([content], [], now, "UTC")

        assert plans == []

    def test_two_hour_gap_drops_closer_reminder(self):
        from src.services.daily_notification.orchestrator import (
            _build_meeting_reminder_plans,
        )
        now = self._now()
        # Two events 30 min apart — both within 3h window, both generate reminders
        # that are 30 min apart. The second should be dropped by the gap constraint.
        event_a = now + timedelta(hours=5)
        event_b = now + timedelta(hours=5, minutes=30)
        events = [
            _event("a", event_a.isoformat()),
            _event("b", event_b.isoformat()),
        ]
        contents = [
            _content("a", notification_type="three_hour_before"),
            _content("b", notification_type="three_hour_before"),
        ]

        plans = _build_meeting_reminder_plans(contents, events, now, "UTC")

        assert len(plans) == 1

    def test_max_reminders_capped_at_three(self):
        from src.services.daily_notification.orchestrator import (
            _build_meeting_reminder_plans,
        )
        now = self._now()
        events = []
        contents = []
        for i in range(6):
            # Space them 4 hours apart so none hit the 2h gap constraint.
            start = now + timedelta(hours=4 + i * 4)
            events.append(_event(f"e{i}", start.isoformat()))
            contents.append(_content(f"e{i}", notification_type="three_hour_before"))

        plans = _build_meeting_reminder_plans(contents, events, now, "UTC")

        assert len(plans) <= 3

    def test_empty_inputs_returns_empty(self):
        from src.services.daily_notification.orchestrator import (
            _build_meeting_reminder_plans,
        )
        plans = _build_meeting_reminder_plans([], [], self._now(), "UTC")
        assert plans == []
