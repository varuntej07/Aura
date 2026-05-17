"""
NotificationPlannerAgent — decides what two notifications to send today.

Reads the user's recent query history, dietary profile, and news headlines,
then generates a morning and evening nudge tailored to their actual patterns.

Rules the LLM must follow (enforced in system prompt + verified by PushNotificationAgent):
  - morning_nudge send time: 08:00–12:00 in user's timezone
  - evening_nudge send time: 17:00–21:00 in user's timezone
  - Topics must be different from each other
  - Topics should not repeat from yesterday
  - Content must be specific — never generic filler
  - If retry_feedback is present, address it directly
"""

from __future__ import annotations

from ..model_provider import ModelProvider
from .models import DailyPlan


_SYSTEM_PROMPT = """You are Buddy, a proactive planning engine. Every morning you review
                    a user's recent activity and plan two push notifications for the day —
                    one in the morning and one in the evening.

                    Your job is to make these notifications feel like they come from a friend who
                    has been paying attention, not a generic wellness app.

                    RULES:
                    1. morning_nudge.send_at_local_time must be between "08:00" and "12:00" (user's timezone)
                    2. evening_nudge.send_at_local_time must be between "17:00" and "21:00" (user's timezone)
                    3. morning_nudge and evening_nudge must be on DIFFERENT topics
                    4. Do not repeat topics from topics_sent_yesterday
                    5. title: ≤ 50 characters, punchy, no corporate speak
                    6. body: ≤ 100 characters, the real message
                    7. opening_chat_message: 1–2 sentences that feel like picking up a conversation
                    8. quick_reply_chips: 2–3 short tappable options, no more
                    9. send_at_utc: convert send_at_local_time to UTC using the user's timezone
                    10. If retry_feedback is provided, read it carefully and fix exactly what it describes

                    CALENDAR RULES:
                    - Do not schedule a nudge within 30 minutes before or after any listed calendar event.
                    - If the user has a relevant event tomorrow or later this week, reference it in nudge content
                      where it adds genuine value. e.g. "You've got a big presentation Thursday" or "Big week ahead."
                    - If the user has 4 or more events in the next 3 days, acknowledge the busy schedule.
                    - Never use em-dashes in title, body, or opening_chat_message. Use commas or new sentences.

                    SIGNAL USAGE:
                    - If recent_queries has 3+ items with a clear pattern → build both nudges around real user behavior
                    - If recent_queries is thin (< 3 items or no pattern) → use news_items to frame a relevant
                    headline around the user's dietary goals; set plan_source to "news_fallback"
                    - Always set plan_source accurately:
                        "query_based"   — you used real query patterns
                        "news_fallback" — you used news items because query signal was thin

                    NEVER say things like:
                    "Stay hydrated!", "Great job!", "As your AI assistant...", "Here's a wellness tip!"
                    These are generic and will be rejected.

                    Return ONLY valid JSON matching this exact structure (no markdown fences):
                    {
                    "morning_nudge": {
                        "topic": "...",
                        "title": "...",
                        "body": "...",
                        "send_at_local_time": "HH:MM",
                        "send_at_utc": "ISO 8601 UTC datetime",
                        "why_this_topic": "...",
                        "opening_chat_message": "...",
                        "quick_reply_chips": ["...", "...", "..."]
                    },
                    "evening_nudge": { ...same structure... },
                    "plan_source": "query_based" | "news_fallback"
                    }
                    """


class NotificationPlannerAgent:
    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    async def generate(self, context: dict) -> DailyPlan:
        """Generate a DailyPlan from the user's context.

        Args:
            context: {
                recent_queries: list[dict],         # last 10 queries, newest first
                dietary_profile: dict | None,
                topics_sent_yesterday: list[str],   # topics from last 2 daily_plans
                news_items: list[dict],             # from rss_client.fetch_news()
                user_timezone: str,                 # IANA e.g. "Asia/Kolkata"
                current_local_datetime: str,        # ISO local datetime string
                retry_feedback: str | None,         # set on retry only
            }

        Returns:
            DailyPlan with morning_nudge and evening_nudge.
        """
        prompt = _build_prompt(context)
        return await self._models.cheap(  # type: ignore[return-value]
            prompt,
            system=_SYSTEM_PROMPT,
            response_model=DailyPlan,
        )


def _build_prompt(context: dict) -> str:
    recent_queries: list[dict] = context.get("recent_queries", [])
    dietary_profile: dict = context.get("dietary_profile") or {}
    topics_sent_yesterday: list[str] = context.get("topics_sent_yesterday", [])
    news_items: list[dict] = context.get("news_items", [])
    upcoming_events: list[dict] = context.get("upcoming_events", [])
    user_timezone: str = context.get("user_timezone", "UTC")
    current_local_datetime: str = context.get("current_local_datetime", "")
    retry_feedback: str | None = context.get("retry_feedback")

    query_summary = _summarise_queries(recent_queries)
    news_summary = _summarise_news(news_items)

    profile_lines = []
    if dietary_profile.get("goal"):
        profile_lines.append(f"Goal: {dietary_profile['goal']}")
    if dietary_profile.get("restrictions"):
        profile_lines.append(f"Dietary restrictions: {', '.join(dietary_profile['restrictions'])}")
    if dietary_profile.get("allergies"):
        profile_lines.append(f"Allergies: {', '.join(dietary_profile['allergies'])}")
    if dietary_profile.get("activity_level"):
        profile_lines.append(f"Activity level: {dietary_profile['activity_level']}")
    profile_text = "\n".join(profile_lines) if profile_lines else "No dietary profile set."

    yesterday_text = (
        f"Topics already sent in the last 2 days: {', '.join(topics_sent_yesterday)}"
        if topics_sent_yesterday
        else "No notifications sent in the last 2 days."
    )

    calendar_summary = _summarise_calendar_events(upcoming_events)

    prompt = f"""Plan today's two notifications for this user.

Current local datetime: {current_local_datetime}
User timezone: {user_timezone}

DIETARY PROFILE:
{profile_text}

RECENT QUERY HISTORY (last 10, newest first):
{query_summary}

CALENDAR (next 7 days — avoid nudge times that fall within 30 min of these events):
{calendar_summary}

RELEVANT NEWS ITEMS (use if query signal is thin):
{news_summary}

{yesterday_text}"""

    if retry_feedback:
        prompt += f"""

IMPORTANT — PREVIOUS ATTEMPT WAS REJECTED:
{retry_feedback}
Fix exactly what is described above. Do not repeat the same mistake."""

    return prompt


def _summarise_queries(queries: list[dict]) -> str:
    if not queries:
        return "No recent queries."
    lines = []
    for q in queries[:10]:
        text = q.get("text", "").strip()
        query_type = q.get("type", "chat")
        timestamp = q.get("timestamp", "")
        if text:
            lines.append(f"  [{query_type}] {text}  ({timestamp[:10] if timestamp else 'unknown date'})")
    return "\n".join(lines) if lines else "No recent queries."


def _summarise_news(news_items: list[dict]) -> str:
    if not news_items:
        return "No news available."
    lines = []
    for item in news_items[:5]:
        title = item.get("title", "")
        summary = item.get("summary", "")
        published = item.get("published_at", "")
        if title:
            line = f"  • {title} ({published})"
            if summary:
                # Truncate long summaries
                short_summary = summary[:150] + "..." if len(summary) > 150 else summary
                line += f"\n    {short_summary}"
            lines.append(line)
    return "\n".join(lines) if lines else "No news available."


def _summarise_calendar_events(events: list[dict]) -> str:
    if not events:
        return "No upcoming calendar events."
    lines = []
    for event in events[:15]:
        title = event.get("title") or "Untitled"
        start = (event.get("start_at") or "")[:16].replace("T", " ")
        attendees = event.get("attendee_count", 0)
        attendee_note = f" ({attendees} attendees)" if attendees else ""
        lines.append(f"  • {start}  {title}{attendee_note}")
    return "\n".join(lines)
