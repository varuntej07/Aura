from __future__ import annotations

from typing import Any

from ...services.model_provider import ModelProvider
from ..agent_base import ScheduledAgent
from ..data_fetchers.job_boards import fetch_remotive_jobs, fetch_adzuna_jobs


class JobsAgent(ScheduledAgent):
    """
    HuntMode — surfaces relevant remote job listings.
    Tone: direct career coach. Cuts to roles that match, skips the noise.
    """

    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    @property
    def agent_id(self) -> str:
        return "jobs"

    async def fetch_data(self, user_config: dict[str, Any]) -> list[dict[str, Any]]:
        search_term = user_config.get("job_title", "machine learning engineer")
        location = user_config.get("location", "us")
        remotive, adzuna = await __import__("asyncio").gather(
            fetch_remotive_jobs(search=search_term, limit=8),
            fetch_adzuna_jobs(what=search_term, where=location, limit=5),
        )
        return [*remotive, *adzuna]

    async def build_notification(
        self,
        user_id: str,
        content: list[dict[str, Any]],
        user_config: dict[str, Any],
        interaction_history: list[dict[str, Any]],
    ) -> dict[str, str]:
        if not content:
            return {
                "title": "HuntMode",
                "body": "No new listings today. Market's quiet.",
                "opening_chat_message": "Nothing new on the boards today — want me to search a different role?",
            }

        job_title = user_config.get("job_title", "your target role")
        skills = user_config.get("skills", [])
        engagement_summary = _summarize_feedback(interaction_history)

        prompt = f"""You are HuntMode, a direct career coach who finds jobs worth applying to.

Target role: {job_title}
Key skills: {', '.join(skills) if skills else 'not specified'}
Recent engagement: {engagement_summary}

Fresh job listings:
{_format_content(content)}

Generate a push notification with this JSON structure:
{{
  "title": "<max 50 chars — include company name or role if notable>",
  "body": "<max 100 chars — specific: name company, role, or location>",
  "opening_chat_message": "<1-2 sentences to discuss the listings, practical tone>"
}}

Rules:
- Name specific companies or roles — no generic "new jobs posted"
- If a listing looks like a strong match for the target role, highlight it
- Return ONLY valid JSON, no markdown.
"""
        result = await self._models.cheap(
            prompt,
            system="You are HuntMode, a direct career coach. Output valid JSON only.",
        )
        return _parse_notification_json(result)


def _summarize_feedback(recent: list[dict]) -> str:
    if not recent:
        return "No history yet"
    tapped = sum(1 for r in recent if r.get("user_action") == "tapped")
    total = len(recent)
    return f"{tapped}/{total} recent nudges engaged with"


def _format_content(content: list[dict]) -> str:
    lines = []
    for item in content[:8]:
        company = item.get("company", "")
        title = item.get("title", "")
        location = item.get("location", "Remote")
        source = item.get("source", "")
        lines.append(f"[{source}] {title} @ {company} — {location}")
    return "\n".join(lines) or "No listings"


def _parse_notification_json(raw: Any) -> dict[str, str]:
    import json
    try:
        if isinstance(raw, dict):
            return raw
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        return json.loads(text)
    except Exception:
        return {
            "title": "HuntMode",
            "body": "New listings dropped. Tap to see what's worth your time.",
            "opening_chat_message": "Found some fresh listings — want me to walk you through them?",
        }
