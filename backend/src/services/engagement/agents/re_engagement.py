"""ReEngagementAgent — generates follow-up notifications when user hasn't responded."""

from __future__ import annotations

from ....prompts import (
    RE_ENGAGEMENT_SYSTEM_PROMPT,
    engagement_reengagement_user_prompt,
)
from ...model_provider import ModelProvider
from ..models import ReEngagementOutput
from .base_agent import BaseAgent


class ReEngagementAgent(BaseAgent):
    def __init__(self, models: ModelProvider) -> None:
        super().__init__(models)

    async def generate(self, context: dict) -> ReEngagementOutput:  # type: ignore[override]
        level: int = context.get("escalation_level", 1)
        original_agent: str = context.get("original_agent", "general")
        original_topic: str = context.get("original_topic", "something we talked about")
        original_title: str = context.get("original_notification_title", "")

        prompt = engagement_reengagement_user_prompt(
            level=level,
            topic=original_topic,
            title=original_title,
            agent=original_agent,
        )

        return await self._models.cheap(
            prompt,
            system=RE_ENGAGEMENT_SYSTEM_PROMPT,
            response_model=ReEngagementOutput,
        )
