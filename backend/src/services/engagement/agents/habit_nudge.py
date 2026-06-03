"""HabitNudgeAgent — calls out patterns in user behaviour (workout intent, sleep, etc.)."""

from __future__ import annotations

from ...model_provider import ModelProvider
from ..models import NotificationOutput
from .base_agent import BaseAgent

_SYSTEM_PROMPT = """You are Buddy. You pay attention to what the user does and does not do.

        Generate a push notification that calls out a behavioural pattern directly.
        Name the exact behaviour — number of times, how many days, what they said vs what they did.
        Sound like a friend who was paying attention, not an analytics dashboard.

        Rules:
          - title: max 50 chars
          - body: max 100 chars — state the specific pattern with real numbers
          - opening_chat_message: 1-2 sentences, opens a real conversation about it
          - suggested_replies: 2-3 short tappable chips
          - Never start with "I noticed" — just say the thing
          - Wry, not preachy. One observation, not a lecture.

        Examples:

        workout_intent_inactive (asked 3 times this week, hasn't gone in 5 days):
          title: "All talk, no gym"
          body: "You asked about working out three times this week. You haven't gone once. Better move ya ass off the couch"
          opening_chat_message: "You've been thinking about the gym a lot but ain't going. What tf is stopping you?"

        late_night_sleep_concern (asked about sleep 4 times after 11pm):
          title: "You keep asking, not sleeping"
          body: "Four sleep questions this week, all after midnight. The answer is the same every time. STFU & go to bed"
          opening_chat_message: "You're clearly not sleeping well. What's keeping you up?"

        Return ONLY valid JSON:
        {
          "title": "...",
          "body": "...",
          "opening_chat_message": "...",
          "suggested_replies": ["...", "...", "..."]
        }
        """


class HabitNudgeAgent(BaseAgent):
    def __init__(self, models: ModelProvider) -> None:
        super().__init__(models)

    async def generate(self, context: dict) -> NotificationOutput:
        signal: str = context.get("signal", "general")
        days_since: int = context.get("days_since", 0)
        query_count: int = context.get("query_count", 0)
        occurrences: int = context.get("occurrences", 0)

        if signal == "workout_intent_inactive":
            detail = (
                f"User asked about working out {query_count} times this week "
                f"but hasn't actually done it in {days_since} days."
            )
        elif signal == "late_night_sleep_concern":
            detail = (
                f"User asked about sleep/insomnia {occurrences} times between 11pm and 3am "
                f"this week."
            )
        else:
            detail = f"Signal: {signal}. Context: {context}"

        prompt = f"""Generate a habit nudge notification for this pattern:

            {detail}

            Be specific, wry, not preachy. Return JSON only.
            """

        return await self._models.cheap(
            prompt,
            system=_SYSTEM_PROMPT,
            response_model=NotificationOutput,
        )
