from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from ...services.model_provider import ModelProvider
from ..data_fetchers.web_search import web_search
from ..agent_base import ScheduledAgent

_DEFAULT_TECH_INTERESTS = ["AI", "ML", "startups", "open source"]
_DAILY_NOTIFICATION_LIMIT = 2
_SIGNIFICANCE_THRESHOLD = 8
_RELEVANCE_THRESHOLD = 7


class TechNewsAgent(ScheduledAgent):
    """
    BytePulse — monitors tech and AI news via web search and judges whether
    anything is worth notifying the user about.

    Judge criteria (all must hold to send a notification):
      - Significance ≥ 8/10: major model release, acquisition, regulatory ruling,
        breakthrough result — blog posts and minor updates do not qualify
      - Relevance ≥ 7/10: matches user's stated interests
      - Novel: not already covered in today's notifications
      - Daily cap: max 2 notifications per day
    """

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

    async def build_notification(
        self,
        user_id: str,
        content: list[dict[str, Any]],
        user_config: dict[str, Any],
        interaction_history: list[dict[str, Any]],  # noqa: ARG002 — judge uses state, not history
    ) -> dict[str, str] | None:
        """
        Judges the fetched content with a single LLM call. Returns a notification
        payload if something passes all criteria, or None to skip.
        """
        if not content:
            return None

        state = await self.load_agent_state(user_id)
        if state["daily_count"] >= _DAILY_NOTIFICATION_LIMIT:
            return None

        interests: list[str] = user_config.get("interests", _DEFAULT_TECH_INTERESTS)
        raw_text: str = content[0].get("text", "")
        current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        verdict = await self._run_judge(
            raw_text=raw_text,
            interests=interests,
            seen_today=state["seen_today"],
            daily_count=state["daily_count"],
            current_date=current_date,
        )

        if verdict is None or verdict.get("decision") == "NO":
            return None

        event_fingerprint = verdict.get("event_fingerprint", f"technews_{current_date}_{state['daily_count']}")

        # save_agent_state is intentionally NOT called here.
        # The orchestrator calls it after confirmed FCM delivery to keep
        # per-agent dedup state and actual send count in sync.
        return {
            "title": verdict.get("title", "BytePulse"),
            "body": verdict.get("body", ""),
            "opening_chat_message": verdict.get("opening_chat_message", ""),
            "event_fingerprint": event_fingerprint,
        }

    async def _run_judge(
        self,
        raw_text: str,
        interests: list[str],
        seen_today: list[str],
        daily_count: int,
        current_date: str,
    ) -> dict[str, Any] | None:
        seen_summary = ", ".join(seen_today) if seen_today else "nothing yet"
        prompt = f"""You are the tech news intelligence judge for Buddy, a personal assistant app.

                User interests: {", ".join(interests)}
                Already notified today: {seen_summary}
                Notifications sent today: {daily_count}/{_DAILY_NOTIFICATION_LIMIT}
                Current date: {current_date}

                Latest tech news (live web fetch):
                {raw_text[:3000]}

                Your job: decide if anything here is worth interrupting the user for.

                Approve ONLY if ALL of the following hold:
                1. Significance ≥ {_SIGNIFICANCE_THRESHOLD}/10 — major model release (GPT-5, Gemini 2, Claude 4), billion-dollar acquisition, landmark regulation, a paper with concrete breakthrough numbers. Opinion pieces, minor updates, and "company announces plans" do NOT qualify.
                2. Relevance ≥ {_RELEVANCE_THRESHOLD}/10 — directly matches the user's stated interests above.
                3. Novel — not the same story or event already covered in today's earlier notifications.

                Don't make user feel bad for sending news that is not relevant or significant. Only if its virally worthy enough.

                COPY EXAMPLES — match this style exactly:
                GOOD title: "OpenAI drops o3 mini — 3x cheaper, same reasoning"
                GOOD body: "o3 mini benchmarks at GPT-4 level on MATH. Available in API today."
                BAD title: "Big AI news!"
                BAD body: "Something exciting happened in tech. Check it out."
                Rule: every title must name the actual thing. Every body must contain at least one number or proper noun. If you can only write a BAD example, return NO instead.

                If something passes, return this JSON:
                {{
                "title": "<max 50 chars, sharp and catchy, name the actual thing>",
                "body": "<max 100 chars — be specific: name the model, company, or number>",
                "opening_chat_message": "<1-2 sentences to open the chat thread with real facts, no hype>",
                "event_fingerprint": "<short unique string identifying this specific story, e.g. 'openai_gpt5_release'>"
                }}

                If nothing qualifies, return exactly:
                {{"decision": "NO"}}

                STRICTLY return JSON only. No markdown. No em-dashes. No explanation outside the JSON.
                """

        raw = await self._models.cheap(
            prompt,
            system="You are user's friend who loves to share tech news with friends. Output valid JSON only.",
        )
        return _parse_judge_response(raw)


# Helpers
def _build_search_queries(interests: list[str], current_date: str) -> list[str]:
    top_interests = " ".join(interests[:2])
    return [
        f"top AI ML tech news breakthroughs across the world today {current_date}",
        f"{top_interests} major announcement release today {current_date}",
    ]


def _parse_judge_response(raw: Any) -> dict[str, Any] | None:
    try:
        if isinstance(raw, dict):
            return raw
        text = str(raw).strip()
        if text.startswith("```"):
            text = text.split("```")[1].lstrip("json").strip()
        return json.loads(text)
    except Exception:
        return None
