from __future__ import annotations

from typing import Any

from ...services.model_provider import ModelProvider
from ..agent_base import ScheduledAgent
from ..data_fetchers.cricket_scores import fetch_recent_results, fetch_live_matches


class CricketAgent(ScheduledAgent):
    """CricBolt — fetches cricket scores and match results."""

    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    @property
    def agent_id(self) -> str:
        return "cricket"

    async def fetch_data(self, user_config: dict[str, Any]) -> list[dict[str, Any]]:
        results, live = await __import__("asyncio").gather(
            fetch_recent_results(limit=5),
            fetch_live_matches(),
        )
        return [*live, *results]

