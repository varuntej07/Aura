from __future__ import annotations

from typing import Any

from ...services.model_provider import ModelProvider
from ..agent_base import ScheduledAgent


class TechNewsAgent(ScheduledAgent):
    """BytePulse — tech/AI content is ingested by the signal engine
    (Hacker News + arXiv + Google News RSS), not here. No external fetch."""

    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    @property
    def agent_id(self) -> str:
        return "technews"

    async def fetch_data(self, user_config: dict[str, Any]) -> list[dict[str, Any]]:
        return []
