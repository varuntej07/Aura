"""HabitNudgeAgent — calls out patterns in user behaviour (workout intent, sleep, etc.)."""

from __future__ import annotations

from ....prompts import HABIT_NUDGE_SYSTEM_PROMPT, habit_nudge_user_prompt
from ...model_provider import ModelProvider
from ..models import NotificationOutput
from .base_agent import BaseAgent


class HabitNudgeAgent(BaseAgent):
    def __init__(self, models: ModelProvider) -> None:
        super().__init__(models)

    async def generate(self, context: dict) -> NotificationOutput:
        signal: str = context.get("signal", "general")
        days_since: int = context.get("days_since", 0)
        query_count: int = context.get("query_count", 0)
        occurrences: int = context.get("occurrences", 0)

        if signal == "workout_intent_inactive":
            detail = (
                f"User asked about working out {query_count} times this week "
                f"but hasn't actually done it in {days_since} days."
            )
        elif signal == "late_night_sleep_concern":
            detail = (
                f"User asked about sleep/insomnia {occurrences} times between 11pm and 3am "
                f"this week."
            )
        else:
            detail = f"Signal: {signal}. Context: {context}"

        prompt = habit_nudge_user_prompt(detail)

        return await self._models.cheap(
            prompt,
            system=HABIT_NUDGE_SYSTEM_PROMPT,
            response_model=NotificationOutput,
        )
