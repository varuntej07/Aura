"""
CalendarNotificationAgent -- classifies upcoming calendar events and generates
personalized notification content for the daily notification pipeline.

Called during the 7 AM planning run. Receives two pre-filtered event lists:
  - events_today: non-all-day events starting today (candidates for three_hour_before)
  - events_three_days_away: non-all-day events starting exactly 3 days from today
                            (high-importance ones get a three_day_ahead heads-up)

Returns a CalendarNotificationBatch. The orchestrator computes send_at_utc
and enforces the global 2-hour gap constraint after this agent returns.

No em-dashes in any generated text field.
"""

from __future__ import annotations

from ...prompts import CALENDAR_NOTIFICATION_SYSTEM_PROMPT, calendar_notification_user_prompt
from ..model_provider import ModelProvider
from .models import CalendarNotificationBatch


class CalendarNotificationAgent:
    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    async def generate_reminders(
        self,
        *,
        events_today: list[dict],
        events_three_days_away: list[dict],
        user_timezone: str,
    ) -> CalendarNotificationBatch:
        """Classify events and generate notification content.

        Returns an empty batch if both event lists are empty.
        """
        if not events_today and not events_three_days_away:
            return CalendarNotificationBatch(reminders=[])

        prompt = calendar_notification_user_prompt(
            events_today=events_today,
            events_three_days_away=events_three_days_away,
            timezone=user_timezone,
        )
        return await self._models.cheap(  # type: ignore[return-value]
            prompt,
            system=CALENDAR_NOTIFICATION_SYSTEM_PROMPT,
            response_model=CalendarNotificationBatch,
        )
