"""
SuggestionPillsAgent — generates 4-5 short suggestion pill labels per agent.

Runs once daily alongside the notification pipeline (triggered from orchestrator.py
after the daily plan is written). Writes results to Firestore:
    agent_suggestion_pills/{user_id}  →  { "sports": [...], "technews": [...], ... }

Each agent's pills are grounded in live context relevant to that agent's domain:
  - sports: today's sports headlines from RSS
  - technews: today's AI/tech headlines from RSS
  - jobs: user's recent query history (job intent signals)
  - posts: user's recent query history (topics + tone signals)

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

_SPORTS_RSS_KEYWORDS = ["sports", "cricket", "football", "IPL"]
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
        interest_subjects: list[str] | None = None,
    ) -> None:
        """Generate and save suggestion pills for all chat agents + main Buddy chat.

        Fetches domain-specific RSS context for sports and technews in parallel
        with the LLM calls. The main Buddy chat is grounded in the user's recent
        queries plus their UserAura interest subjects (passed in already
        consent-gated). Writes results to agent_suggestion_pills/{user_id}.
        Errors per agent are caught individually so one failure doesn't block others.
        """
        sports_result, tech_result = await asyncio.gather(
            rss_client.fetch_news(_SPORTS_RSS_KEYWORDS),
            rss_client.fetch_news(_TECHNEWS_RSS_KEYWORDS),
            return_exceptions=True,
        )

        sports_news = _news_items_or_empty("sports", sports_result)
        tech_news = _news_items_or_empty("technews", tech_result)

        results = await asyncio.gather(
            self._generate_pills_for_agent("sports", sports_news, recent_queries),
            self._generate_pills_for_agent("technews", tech_news, recent_queries),
            self._generate_pills_for_agent("posts", [], recent_queries),
            self._generate_pills_for_agent(
                "buddy", [], recent_queries, interest_subjects
            ),
            return_exceptions=True,
        )

        agent_ids = ["sports", "technews", "posts", "buddy"]
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
            # The daily run owns the whole doc, so stamp buddy_generated_at too when
            # the buddy set is part of this write — the on-demand refresher reads it
            # to decide staleness.
            extra = (
                {"buddy_generated_at": datetime.now(UTC).isoformat()}
                if "buddy" in pills_by_agent_id
                else None
            )
            await _write_suggestion_pills(user_id, pills_by_agent_id, extra)

    async def generate_buddy_pills(
        self,
        user_id: str,
        recent_queries: list[dict],
        interest_subjects: list[str] | None = None,
    ) -> list[str]:
        """Regenerate ONLY the main Buddy chat pills and merge them into the doc.

        Used by the on-demand refresh endpoint (fired when the user leaves the app
        after a text or voice session). Merges so the agent pill sets written by the
        daily run are never clobbered. Returns the pills (empty list on failure).
        """
        pills = await self._generate_pills_for_agent(
            "buddy", [], recent_queries, interest_subjects
        )
        if pills:
            await _write_buddy_pills(user_id, pills)
        return pills

    async def _generate_pills_for_agent(
        self,
        agent_id: str,
        news_items: list[dict],
        recent_queries: list[dict],
        interest_subjects: list[str] | None = None,
    ) -> list[str]:
        prompt = _build_prompt(agent_id, news_items, recent_queries, interest_subjects)
        raw: str = await self._models.cheap(prompt, system=_SYSTEM_PROMPT)
        return _parse_pills(raw, agent_id)


def _build_prompt(
    agent_id: str,
    news_items: list[dict],
    recent_queries: list[dict],
    interest_subjects: list[str] | None = None,
) -> str:
    agent_descriptions = {
        "sports": (
            "MatchPoint: a sports analyst covering cricket, football, and more — "
            "scores, player stats, results, and fixtures."
        ),
        "technews": (
            "BytePulse: an AI and tech news curator covering ML research, "
            "developer tools, and the tech industry."
        ),
        "posts": (
            "Tweeter: a social media writing assistant drafting tweets "
            "and posts for X/Twitter."
        ),
        "buddy": (
            "Buddy: the user's personal AI companion for anything — reminders, "
            "plans, decisions, questions, and picking up wherever they left off."
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

    if interest_subjects:
        lines.append("Things this user cares about (their interests):")
        for subject in interest_subjects[:5]:
            lines.append(f"  - {subject}")
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

    if agent_id == "buddy":
        # Buddy is general-purpose; ground the pills in the user's own world so a
        # returning user sees threads worth picking back up, not generic prompts.
        # CRITICAL: tapping a pill drops the text into the user's input box, so each
        # pill must read as something the USER types TO Buddy — never as a question
        # Buddy asks the user (that inverts the meaning and reads as nonsense).
        lines.append(
            "Generate exactly 3 chat starters, each written in the first person, "
            "word-for-word as THIS user would type it to Buddy, 3-6 words each.\n"
            "COMPOSITION (in this order):\n"
            "  1 & 2: pick up a REAL ongoing thread from the interests and recent "
            "queries above — a project, goal, or curiosity they'd actually want to "
            "continue.\n"
            "  3: something FRESH and unexpected, NOT drawn from their history — a "
            "serendipitous starter that sparks a brand-new conversation.\n"
            "PHRASING: write each like the user is texting a close friend — "
            "conversational, not a terse search query. Prefer \"ways to build iOS "
            "without a Mac\" over \"How to build iOS without Mac?\". Keep it under "
            "6 words so it stays tappable.\n"
            "IGNORE one-off errands, logistics, or passing mentions (e.g. \"going "
            "to the bank tomorrow\", \"pick up groceries\") — those are not threads "
            "worth resurfacing. Only the real projects, goals, and curiosities.\n"
            "GOOD (user's voice): \"Help me prep for my interview\", \"Hold me to "
            "the gym today\", \"I'm stuck on the React bug again\".\n"
            "BAD — reject anything phrased as Buddy talking to the user: \"What's "
            "on your plate today?\", \"Catch me up\", \"How can I help?\", \"Need "
            "anything?\"."
        )
    else:
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
    extra: dict[str, Any] | None = None,
) -> None:
    def _write() -> None:
        db = admin_firestore()
        doc: dict[str, Any] = {
            **pills_by_agent_id,
            **(extra or {}),
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


async def _write_buddy_pills(user_id: str, pills: list[str]) -> None:
    """Merge just the buddy pill set + freshness stamp, leaving the agent sets
    (sports/technews/posts) written by the daily run untouched."""
    def _write() -> None:
        db = admin_firestore()
        now_iso = datetime.now(UTC).isoformat()
        db.collection("agent_suggestion_pills").document(user_id).set(
            {"buddy": pills, "buddy_generated_at": now_iso, "updated_at": now_iso},
            merge=True,
        )

    try:
        await asyncio.to_thread(_write)
        logger.info("suggestion_pills: buddy pills refreshed", {
            "user_id": user_id,
            "count": len(pills),
        })
    except Exception as exc:
        logger.exception("suggestion_pills: failed to write buddy pills", {
            "user_id": user_id,
            "error": str(exc),
        })
