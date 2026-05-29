from __future__ import annotations

from typing import Any

from ...services.model_provider import ModelProvider
from ..agent_base import ScheduledAgent


class PostsAgent(ScheduledAgent):
    """Tweeter — synthesizes user activity; no external fetch needed."""

    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    @property
    def agent_id(self) -> str:
        return "posts"

    async def fetch_data(self, user_config: dict[str, Any]) -> list[dict[str, Any]]:
        """Tweeter synthesizes from user's stored interactions, no external fetch needed."""
        return []

