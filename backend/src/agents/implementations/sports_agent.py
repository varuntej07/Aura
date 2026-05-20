from __future__ import annotations

import asyncio
from typing import Any

from ...services.model_provider import ModelProvider
from ..data_fetchers.web_search import web_search
from ..agent_base import ScheduledAgent

_DEFAULT_SPORTS_INTERESTS = ["RCB", "IPL", "India cricket", "Virat Kohli"]


class SportsAgent(ScheduledAgent):
    """SportsDesk — fetches live sports news via web search."""

    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    @property
    def agent_id(self) -> str:
        return "sports"

    async def fetch_data(self, user_config: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Builds targeted search queries from the user's sports interests and runs
        them in parallel. Returns combined grounded text from all queries.
        """
        interests: list[str] = user_config.get("sports_interests", _DEFAULT_SPORTS_INTERESTS)
        queries = _build_search_queries(interests)

        results = await asyncio.gather(
            *[web_search(q, uid="sports_agent") for q in queries],
            return_exceptions=True,
        )

        combined_text = "\n\n".join(
            r for r in results if isinstance(r, str) and r.strip()
        )
        if not combined_text:
            return []

        return [{"text": combined_text, "source": "web_search", "queries": queries}]



# Helpers
def _build_search_queries(interests: list[str]) -> list[str]:
    """
    Derives targeted web search queries from the user's sports interest list.
    Caps at 3 queries to keep parallel fetch latency bounded.
    """
    if not interests:
        return ["livesports match results today viral"]

    queries: list[str] = []

    # Primary: combine top interests into one rich query
    top = " ".join(interests[:3])
    queries.append(f"{top} match result score highlights today")

    # Secondary: live/latest angle
    queries.append(f"{interests[0]} latest news score today")

    # Tertiary: broader sports sweep if the user has varied interests
    if len(interests) > 3:
        queries.append(f"{interests[3]} game result today")

    return queries[:3]
