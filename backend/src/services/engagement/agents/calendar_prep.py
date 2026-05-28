"""CalendarPrepAgent — generates pre-meeting prep notifications."""

from __future__ import annotations

from ...model_provider import ModelProvider
from ..models import NotificationOutput
from .base_agent import BaseAgent

_SYSTEM_PROMPT = """You are Buddy. There's a meeting coming up for the user.

Generate a prep notification. Extract what they actually need to do or know — don't just
repeat the event title. If there's no description, keep it brief and time-aware.

Rules:
  - title: max 50 chars, time-aware ("in 2h", "in 90 min")
  - body: max 100 chars — the one thing they need to act on
  - opening_chat_message: offer to help them prep (1-2 sentences)
  - suggested_replies: 2-3 chips ("Help me prep", "I'm ready", "What should I ask?")

Return ONLY valid JSON:
{
  "title": "...",
  "body": "...",
  "opening_chat_message": "...",
  "suggested_replies": ["...", "...", "..."]
}"""


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

        prompt = f"""Generate a calendar prep notification.

Event: {title}
Time until event: {time_str}
Description: {description or "(none)"}
Attendees: {", ".join(attendees[:5]) or "(none listed)"}

Return JSON only."""

        return await self._models.cheap(
            prompt,
            system=_SYSTEM_PROMPT,
            response_model=NotificationOutput,
        )
