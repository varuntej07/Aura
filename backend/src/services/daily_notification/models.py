"""
Pydantic models for calendar and meeting notification planning.

CalendarNotificationContent is the output of CalendarNotificationAgent.
MeetingReminderPlan is stored in daily_plans and scheduled via Cloud Tasks.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class CalendarNotificationContent(BaseModel):
    model_config = ConfigDict(strict=True)

    event_id: str
    """Provider event ID from the calendar_events collection."""

    event_title: str
    """Original event title, used for idempotency checks."""

    importance_tier: Literal["high", "medium"]
    """high: interviews, reviews, demos, medical. medium: team syncs, standups, 1:1s."""

    notification_type: Literal["three_day_ahead", "three_hour_before"]
    """three_day_ahead fires today for a high-importance event 3 days out.
    three_hour_before fires 3 hours before the event starts."""

    title: str
    """Notification title. Max 50 characters. No em-dashes."""

    body: str
    """Notification body. Max 100 characters. No em-dashes."""

    opening_chat_message: str
    """First message Buddy sends when the user taps the notification. 1-2 sentences."""

    quick_reply_chips: list[str]
    """2-3 short tappable reply options."""

    why_this_notification: str
    """Internal rationale — not shown to the user."""


class CalendarNotificationBatch(BaseModel):
    model_config = ConfigDict(strict=True)

    reminders: list[CalendarNotificationContent]
    """Ordered list of notifications to schedule. May be empty."""


class MeetingReminderPlan(BaseModel):
    model_config = ConfigDict(strict=True)

    event_id: str
    event_title: str
    importance_tier: Literal["high", "medium"]
    notification_type: Literal["three_day_ahead", "three_hour_before"]
    title: str
    body: str
    send_at_utc: str
    """ISO 8601 UTC datetime computed by the orchestrator from event start time."""
    opening_chat_message: str
    quick_reply_chips: list[str]
    why_this_notification: str
