"""CalendarPrepAgent — generates pre-meeting prep notifications."""

from __future__ import annotations

from ....prompts import CALENDAR_PREP_SYSTEM_PROMPT, calendar_prep_user_prompt
from ...model_provider import ModelProvider
from ..models import NotificationOutput
from .base_agent import BaseAgent


class CalendarPrepAgent(BaseAgent):
    def __init__(self, models: ModelProvider) -> None:
        super().__init__(models)

    async def generate(self, context: dict) -> NotificationOutput:
        title: str = context.get("event_title", "Meeting")
        minutes: int = context.get("minutes_until", 120)
        description: str = context.get("description", "")
        attendees: list = context.get("attendees", [])

        hours = minutes // 60
        mins = minutes % 60
        time_str = f"{hours}h {mins}min" if hours else f"{mins} min"

        prompt = calendar_prep_user_prompt(
            title=title,
            time_until=time_str,
            description=description,
            attendees=attendees,
        )

        return await self._models.cheap(
            prompt,
            system=CALENDAR_PREP_SYSTEM_PROMPT,
            response_model=NotificationOutput,
        )
