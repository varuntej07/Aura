"""
CalendarNotificationAgent -- classifies upcoming calendar events and generates
personalized notification content for the daily notification pipeline.

Called during the 7 AM planning run. Receives two pre-filtered event lists:
  - events_today: non-all-day events starting today (candidates for three_hour_before)
  - events_three_days_away: non-all-day events starting exactly 3 days from today
                            (high-importance ones get a three_day_ahead heads-up)

Returns a CalendarNotificationBatch. The orchestrator computes send_at_utc
and enforces the global 2-hour gap constraint after this agent returns.

No em-dashes in any generated text field.
"""

from __future__ import annotations

from ..model_provider import ModelProvider
from .models import CalendarNotificationBatch


_SYSTEM_PROMPT = """You are Buddy, a user's personal assistant reviewing a user's upcoming calendar events.
                Your job: decide which events warrant a proactive notification, classify their importance,
                and write the notification content.

                IMPORTANCE TIERS:
                High -- Interviews, performance reviews, presentations, demos, client pitches,
                        medical appointments, exams, deadline-critical syncs.
                Medium -- Regular team syncs, standups, 1:1s with colleagues.
                Low -- Lunch breaks, personal blocks, OOO, all-day events, travel holds,
                        vague "block" or "hold" entries, birthday reminders.
                        SKIP low-importance events entirely -- return nothing for them.

                NOTIFICATION TYPES:
                three_hour_before -- For high AND medium events starting today.
                                    Fires 3 hours before the event.
                three_day_ahead -- For HIGH-importance events starting in 3 days ONLY.
                                    A preparation heads-up. Do NOT use for medium events.

                CONTENT RULES:
                1. title: max 50 characters. friendly, casual. No corporate tone.
                2. body: max 100 characters. Specific, not generic.
                3. opening_chat_message: 1-2 sentences. Feels like picking up a conversation.
                4. quick_reply_chips: 2-3 short options. e.g. ["Prep me", "I'm ready", "What time is it?"]
                5. NEVER use em-dashes (--) anywhere. Use commas or new sentences instead.
                6. NEVER say "Great!", "As your AI assistant", or any filler phrases.
                7. Reference the actual event title and context. Be specific.
                8. For three_day_ahead: frame it as preparation time, not just a heads-up.
                9. For three_hour_before: convey urgency without panic.

                Return ONLY valid JSON matching this exact structure (no markdown fences):
                {
                "reminders": [
                    {
                    "event_id": "...",
                    "event_title": "...",
                    "importance_tier": "high" | "medium",
                    "notification_type": "three_day_ahead" | "three_hour_before",
                    "title": "...",
                    "body": "...",
                    "opening_chat_message": "...",
                    "quick_reply_chips": ["...", "..."],
                    "why_this_notification": "..."
                    }
                ]
                }

                Return {"reminders": []} if no events are worth notifying about.
            """


class CalendarNotificationAgent:
    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    async def generate_reminders(
        self,
        *,
        events_today: list[dict],
        events_three_days_away: list[dict],
        user_timezone: str,
    ) -> CalendarNotificationBatch:
        """Classify events and generate notification content.

        Returns an empty batch if both event lists are empty.
        """
        if not events_today and not events_three_days_away:
            return CalendarNotificationBatch(reminders=[])

        prompt = _build_prompt(events_today, events_three_days_away, user_timezone)
        return await self._models.cheap(  # type: ignore[return-value]
            prompt,
            system=_SYSTEM_PROMPT,
            response_model=CalendarNotificationBatch,
        )


def _build_prompt(
    events_today: list[dict],
    events_three_days_away: list[dict],
    user_timezone: str,
) -> str:
    lines: list[str] = [f"User timezone: {user_timezone}", ""]

    if events_today:
        lines.append(
            "EVENTS STARTING TODAY"
            " (classify and generate three_hour_before for high and medium importance):"
        )
        for event in events_today:
            lines.append(_format_event(event))
        lines.append("")

    if events_three_days_away:
        lines.append(
            "EVENTS STARTING IN 3 DAYS"
            " (generate three_day_ahead only for high-importance events):"
        )
        for event in events_three_days_away:
            lines.append(_format_event(event))
        lines.append("")

    lines.append("Return notification content only for events that warrant it.")
    return "\n".join(lines)


def _format_event(event: dict) -> str:
    parts = [
        f'  ID: {event.get("id", "")}',
        f'  Title: {event.get("title") or "Untitled"}',
        f'  Start: {event.get("start_at") or "unknown"}',
    ]
    attendee_count = event.get("attendee_count", 0)
    if attendee_count:
        parts.append(f"Attendees: {attendee_count}")
    description = (event.get("description") or "").strip()[:150]
    if description:
        parts.append(f"Description: {description}")
    return "\n".join(parts)
