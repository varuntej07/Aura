from __future__ import annotations

from typing import Any

from ...services.model_provider import ModelProvider
from ..agent_base import ScheduledAgent


class PostsAgent(ScheduledAgent):
    """
    PostForge — drafts tweet-length posts based on what the user has been reading/discussing.
    Tone: the user's voice, amplified. Opinionated, not generic.
    """

    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    @property
    def agent_id(self) -> str:
        return "posts"

    async def fetch_data(self, user_config: dict[str, Any]) -> list[dict[str, Any]]:
        """PostForge synthesizes from user's stored interactions, no external fetch needed."""
        return []

    async def build_notification(
        self,
        user_id: str,
        content: list[dict[str, Any]],
        user_config: dict[str, Any],
        interaction_history: list[dict[str, Any]],
    ) -> dict[str, str] | None:
        topics = _extract_topics_from_feedback(interaction_history)
        tone = user_config.get("tone", "thoughtful and direct")
        niche = user_config.get("niche", "tech")

        if not topics:
            return None

        prompt = f"""You are PostForge, a ghostwriter who captures a person's authentic voice for social media.

User's niche: {niche}
Preferred tone: {tone}
Recent topics from their conversations: {', '.join(topics[:5])}

Draft a tweet-length post (under 280 chars) on the most interesting topic.
Then generate a push notification to show them the draft.

COPY EXAMPLES — match this style exactly:
GOOD title: "Draft ready: your take on Claude 4 agents"
GOOD body: "You've been talking about AI agents all week. Here's a post idea."
BAD title: "PostForge has a draft"
BAD body: "Tap to review."
Rule: title must reference the actual topic. Body must say WHY this is for them specifically. If you can only write a BAD example, return NO instead.

Return this JSON structure:
{{
  "title": "<max 50 chars — reference the actual topic, not the tool name>",
  "body": "<the actual tweet draft, under 140 chars for the notification preview>",
  "opening_chat_message": "<present the full draft (up to 280 chars) and ask if they want tweaks>"
}}

Rules:
- Write in first person as if it's the user's own thought
- Be specific and opinionated — not a generic observation
- Return ONLY valid JSON, no markdown.
- If the draft would be generic or weak, return exactly: {{"decision": "NO"}}
"""
        result = await self._models.cheap(
            prompt,
            system="You are PostForge, a ghostwriter for social media. Output valid JSON only.",
        )
        parsed = _parse_notification_json(result)
        if parsed is None or parsed.get("decision") == "NO":
            return None
        return parsed


def _extract_topics_from_feedback(recent: list[dict]) -> list[str]:
    """Pull content_topic strings from stored interactions."""
    seen: set[str] = set()
    topics: list[str] = []
    for item in recent:
        topic = item.get("content_topic", "")
        if topic and topic not in seen:
            seen.add(topic)
            topics.append(topic)
    return topics


def _parse_notification_json(raw: Any) -> dict[str, str] | None:
    import json
    try:
        if isinstance(raw, dict):
            return raw
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        return json.loads(text)
    except Exception:
        return None  # unparseable output → suppress; never fall back to generic copy
