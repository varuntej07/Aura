from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any

from ...services.model_provider import ModelProvider
from ..data_fetchers.web_search import web_search
from ..agent_base import ScheduledAgent

_DEFAULT_SPORTS_INTERESTS = ["RCB", "IPL", "India cricket", "Virat Kohli"]
_DAILY_NOTIFICATION_LIMIT = 2
_SIGNIFICANCE_THRESHOLD = 8
_RELEVANCE_THRESHOLD = 7


class SportsAgent(ScheduledAgent):
    """
    SportsDesk — monitors live sports news via web search and judges whether
    anything is worth notifying the user about.

    Judge criteria (all must hold to send a notification):
      - Significance ≥ 8/10: century, 5-wicket haul, last-ball win, record, final, major upset
      - Relevance ≥ 7/10: involves user's followed teams or players
      - Novel: not already covered in today's notifications
      - Daily cap: max 2 notifications per day
    """

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

    async def build_notification(
        self,
        user_id: str,
        content: list[dict[str, Any]],
        user_config: dict[str, Any],
        interaction_history: list[dict[str, Any]],  # noqa: ARG002 — judge uses state, not history
    ) -> dict[str, str] | None:
        """
        Judges the fetched content with a single LLM call. Returns a notification
        payload if something passes all criteria, or None to skip the notification.
        """
        if not content:
            return None

        state = await self.load_agent_state(user_id)
        if state["daily_count"] >= _DAILY_NOTIFICATION_LIMIT:
            return None

        interests: list[str] = user_config.get("sports_interests", _DEFAULT_SPORTS_INTERESTS)
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

        event_fingerprint = verdict.get("event_fingerprint", f"sports_{current_date}_{state['daily_count']}")

        # save_agent_state is intentionally NOT called here.
        # The orchestrator calls it after confirmed FCM delivery to keep
        # per-agent dedup state and actual send count in sync.
        return {
            "title": verdict.get("title", "SportsDesk"),
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
        prompt = f"""You are users friend who loves to share exciting sport news with friends. 

            User follows: {", ".join(interests)}
            Already notified today: {seen_summary}
            Notifications sent today: {daily_count}/{_DAILY_NOTIFICATION_LIMIT}
            Current date: {current_date}

            Latest sports news (live web fetch):
            {raw_text[:3000]}

            Your job: decide if anything here is worth interrupting the user for.

            Approve ONLY if ALL of the following hold:
            1. Significance ≥ {_SIGNIFICANCE_THRESHOLD}/10 — century, 5-wicket haul, last-ball win, record broken, tournament final, major upset. Routine wins and weather updates do NOT qualify.
            2. Relevance ≥ {_RELEVANCE_THRESHOLD}/10 — directly involves the user's followed teams or players listed above.
            3. Novel — not the same match or event already covered in today's earlier notifications.

            Do not make user dislike you for sending a news that's not exciting or viral.

            COPY EXAMPLES — match this style exactly:
            GOOD title: "Kohli 96* — one shot from a century vs MI"
            GOOD body: "RCB chasing 187. Kohli and du Plessis at the crease. 12 off 8."
            BAD title: "Cricket update!"
            BAD body: "There's an exciting match happening. Tap to see more."
            Rule: name the actual players, teams, and scores. If you don't have specifics, return NO.

            If something passes, return this JSON:
            {{
            "title": "<max 50 chars, punchy — name the actual players or teams>",
            "body": "<max 100 chars — name the actual players, scores, or teams>",
            "opening_chat_message": "<1-2 sentences opening the chat with real match facts, no hype>",
            "event_fingerprint": "<short unique string identifying this specific event, e.g. 'rcb_mi_may13'>"
            }}

            If nothing qualifies, return exactly:
            {{"decision": "NO"}}

            JSON only. No markdown. No explanation outside the JSON.
            """

        raw = await self._models.cheap(
            prompt,
            system="You are a friend who is excited to share sports news with user. Output valid JSON only.",
        )
        return _parse_judge_response(raw)


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
