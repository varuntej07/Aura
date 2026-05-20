from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from ...services.model_provider import ModelProvider
from ..data_fetchers.web_search import web_search
from ..agent_base import ScheduledAgent

_DEFAULT_TECH_INTERESTS = ["AI", "ML", "startups", "open source"]


class TechNewsAgent(ScheduledAgent):
    """BytePulse — fetches tech and AI news via web search."""

    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    @property
    def agent_id(self) -> str:
        return "technews"

    async def fetch_data(self, user_config: dict[str, Any]) -> list[dict[str, Any]]:
        """
        Runs two parallel web searches — a broad tech news sweep and an
        interest-specific query — and returns combined grounded text.
        """
        interests: list[str] = user_config.get("interests", _DEFAULT_TECH_INTERESTS)
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        queries = _build_search_queries(interests, current_date)

        results = await asyncio.gather(
            *[web_search(q, uid="technews_agent") for q in queries],
            return_exceptions=True,
        )

        combined_text = "\n\n".join(
            r for r in results if isinstance(r, str) and r.strip()
        )
        if not combined_text:
            return []

        return [{"text": combined_text, "source": "web_search", "queries": queries}]



# Helpers
def _build_search_queries(interests: list[str], current_date: str) -> list[str]:
    top_interests = " ".join(interests[:2])
    return [
        f"top AI ML tech news breakthroughs across the world today {current_date}",
        f"{top_interests} major announcement release today {current_date}",
    ]
