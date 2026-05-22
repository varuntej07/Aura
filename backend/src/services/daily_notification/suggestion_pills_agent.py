"""
SuggestionPillsAgent — generates 4-5 short suggestion pill labels per agent.

Runs once daily alongside the notification pipeline (triggered from orchestrator.py
after the daily plan is written). Writes results to Firestore:
    agent_suggestion_pills/{user_id}  →  { "cricket": [...], "technews": [...], ... }

Each agent's pills are grounded in live context relevant to that agent's domain:
  - cricket:   today's cricket/IPL headlines from RSS
  - technews:  today's AI/tech headlines from RSS
  - jobs:      user's recent query history (job intent signals)
  - posts:     user's recent query history (topics + tone signals)

Pills are 3-6 words each — short enough to fit in a scrollable chip row.
On any failure the agent is skipped silently; the Flutter app falls back to
hardcoded defaults for missing agents.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any

from ...lib.logger import logger
from ...services.firebase import admin_firestore
from ...services.model_provider import ModelProvider
from . import rss_client

_CRICKET_RSS_KEYWORDS = ["cricket", "IPL", "cricket match"]
_TECHNEWS_RSS_KEYWORDS = ["artificial intelligence", "machine learning", "tech startup"]

_SYSTEM_PROMPT = """You generate short suggestion pill labels for a chat agent.
Pills are 3-6 words each, written from the user's perspective as something they would type.
Return ONLY a JSON array of strings. No markdown, no explanation.
Example: ["IPL today", "Top scorer", "Points table", "Next match", "Player stats"]"""


class SuggestionPillsAgent:
    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    async def generate_all_agent_suggestion_pills(
        self,
        user_id: str,
        recent_queries: list[dict],
    ) -> None:
        """Generate and save suggestion pills for all 4 chat agents.

        Fetches domain-specific RSS context for cricket and technews in parallel
        with the LLM calls. Writes results to agent_suggestion_pills/{user_id}.
        Errors per agent are caught individually so one failure doesn't block others.
        """
        cricket_result, tech_result = await asyncio.gather(
            rss_client.fetch_news(_CRICKET_RSS_KEYWORDS),
            rss_client.fetch_news(_TECHNEWS_RSS_KEYWORDS),
            return_exceptions=True,
        )

        cricket_news = _news_items_or_empty("cricket", cricket_result)
        tech_news = _news_items_or_empty("technews", tech_result)

        results = await asyncio.gather(
            self._generate_pills_for_agent("cricket", cricket_news, recent_queries),
            self._generate_pills_for_agent("technews", tech_news, recent_queries),
            self._generate_pills_for_agent("jobs", [], recent_queries),
            self._generate_pills_for_agent("posts", [], recent_queries),
            return_exceptions=True,
        )

        agent_ids = ["cricket", "technews", "jobs", "posts"]
        pills_by_agent_id: dict[str, list[str]] = {}

        for agent_id, result in zip(agent_ids, results):
            if isinstance(result, Exception):
                logger.warn("suggestion_pills: generation failed for agent", {
                    "agent_id": agent_id,
                    "error": str(result),
                })
            elif isinstance(result, list) and result:
                pills_by_agent_id[agent_id] = result

        if pills_by_agent_id:
            await _write_suggestion_pills(user_id, pills_by_agent_id)

    async def _generate_pills_for_agent(
        self,
        agent_id: str,
        news_items: list[dict],
        recent_queries: list[dict],
    ) -> list[str]:
        prompt = _build_prompt(agent_id, news_items, recent_queries)
        raw: str = await self._models.cheap(prompt, system=_SYSTEM_PROMPT)
        return _parse_pills(raw, agent_id)


def _build_prompt(
    agent_id: str,
    news_items: list[dict],
    recent_queries: list[dict],
) -> str:
    agent_descriptions = {
        "cricket": (
            "CricBolt: a cricket analyst covering IPL, Test matches, "
            "player stats, scores, and fixtures."
        ),
        "technews": (
            "BytePulse: an AI and tech news curator covering ML research, "
            "developer tools, and the tech industry."
        ),
        "jobs": "HuntMode: a job search assistant surfacing software engineering and AI/ML roles.",
        "posts": (
            "Tweeter: a social media writing assistant drafting tweets "
            "and posts for X/Twitter."
        ),
    }
    description = agent_descriptions.get(agent_id, agent_id)

    lines = [f"Agent: {description}", ""]

    if news_items:
        lines.append("Today's relevant headlines:")
        for item in news_items[:5]:
            title = item.get("title", "")
            if title:
                lines.append(f"  • {title}")
        lines.append("")

    relevant_queries = [
        q.get("text", "").strip()
        for q in recent_queries[:10]
        if q.get("text", "").strip()
    ]
    if relevant_queries:
        lines.append("User's recent queries (use for context, not literally):")
        for q in relevant_queries[:5]:
            lines.append(f"  - {q}")
        lines.append("")

    lines.append(
        "Generate 5 suggestion pills this user would tap to start a conversation "
        "with this agent today."
    )
    return "\n".join(lines)


def _parse_pills(raw: str, agent_id: str) -> list[str]:
    """Parse a JSON array of strings from the LLM response. Returns empty list on failure."""
    try:
        cleaned = raw.strip()
        # Strip markdown fences if present
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        pills = json.loads(cleaned)
        if isinstance(pills, list):
            valid = [
                p.strip()
                for p in pills
                if isinstance(p, str) and p.strip() and len(p.strip().split()) <= 6
            ]
            return valid[:5]
    except Exception as exc:
        logger.warn("suggestion_pills: failed to parse LLM response", {
            "agent_id": agent_id,
            "error": str(exc),
            "raw_preview": raw[:100],
        })
    return []


def _news_items_or_empty(agent_id: str, result: object) -> list[dict]:
    if isinstance(result, Exception):
        logger.warn("suggestion_pills: RSS fetch failed", {
            "agent_id": agent_id,
            "error": str(result),
        })
        return []
    if isinstance(result, list):
        return [item for item in result if isinstance(item, dict)]
    return []


async def _write_suggestion_pills(
    user_id: str,
    pills_by_agent_id: dict[str, list[str]],
) -> None:
    def _write() -> None:
        db = admin_firestore()
        doc: dict[str, Any] = {
            **pills_by_agent_id,
            "updated_at": datetime.now(UTC).isoformat(),
        }
        db.collection("agent_suggestion_pills").document(user_id).set(doc)

    try:
        await asyncio.to_thread(_write)
        logger.info("suggestion_pills: written to Firestore", {
            "user_id": user_id,
            "agents": list(pills_by_agent_id.keys()),
        })
    except Exception as exc:
        logger.exception("suggestion_pills: failed to write to Firestore", {
            "user_id": user_id,
            "error": str(exc),
        })
