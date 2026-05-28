"""
DecisionEngine — pure Python, zero LLM calls.

Decides whether to engage the user after a reactive trigger (nutrition scan or
calendar event), which agent to use, and what tone to strike.

Habit nudges are NOT handled here — they are planned daily by NotificationPlannerAgent
in src/services/daily_notification/ which reads actual query history.

Tone selection for nutrition_followup:
    past_scan_count == 0                 → "educate"  (first time, explain)
    1 <= past_scan_count < 3, bad verdict → "warn"    (seen before, heads up)
    past_scan_count >= 3, bad verdict    → "roast"    (they keep doing it)
    verdict == "eat"                     → "celebrate" (good choice)

Rate-limit rules (all re-checked atomically via Firestore transaction in orchestrator.py):
    min 2h between any reactive notifications
    max 2 proactive notifications per day (daily planner owns this budget)
    quiet hours: 10 PM–8 AM in the user's local timezone
    suppress if last query was < 5 min ago (user is currently active in the app)
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import EngagementDecision


# ── Config ────────────────────────────────────────────────────────────────────
# These are module-level so orchestrator.py can reference them in its transaction.

MIN_HOURS_BETWEEN_REACTIVE_NOTIFICATIONS = 2
MAX_DAILY_PROACTIVE_NOTIFICATIONS = 2
SUPPRESS_IF_USER_ACTIVE_WITHIN_MINUTES = 5

QUIET_HOUR_START = 22   # 10 PM in the user's local timezone
QUIET_HOUR_END = 8      #  8 AM in the user's local timezone

# Fixed delays (seconds) for reactive notification types
NUTRITION_FOLLOWUP_DELAY_MINUTES = 45
CALENDAR_PREP_DELAY_MINUTES = 2


# ── Public entry point ────────────────────────────────────────────────────────

def decide(
    trigger_event: str,
    trigger_payload: dict[str, Any],
    context: dict[str, Any],
) -> EngagementDecision:
    """
    Pure function. No I/O. Takes assembled context, returns EngagementDecision.

    Args:
        trigger_event:   "nutrition_scan" | "calendar_event"
        trigger_payload: raw event data (nutrition log doc or calendar event dict)
        context: {
            recent_nutrition_logs: list[dict],
            dietary_profile: dict | None,
            engagement_guard: dict | None,
            memories: list[dict],
            user_timezone: str,               # IANA timezone e.g. "Asia/Kolkata"
        }
    """
    now = datetime.now(UTC)
    guard = context.get("engagement_guard") or {}
    user_timezone = context.get("user_timezone", "UTC")

    # ── Hard gate: rate-limit and quiet-hours checks ──────────────────────────
    suppression = _check_suppression(now, guard, user_timezone)
    if suppression:
        return EngagementDecision(
            should_engage=False,
            chosen_agent="none",
            delay_minutes=0,
            tone="check_in",
            engagement_context={},
            suppression_reason=suppression,
        )

    # ── Route by trigger event ────────────────────────────────────────────────
    if trigger_event == "nutrition_scan":
        return _decide_nutrition(trigger_payload, context)

    if trigger_event == "calendar_event":
        return _decide_calendar(trigger_payload, context)

    return EngagementDecision(
        should_engage=False,
        chosen_agent="none",
        delay_minutes=0,
        tone="check_in",
        engagement_context={},
        suppression_reason=f"unrecognised_trigger:{trigger_event}",
    )


# ── Suppression checks ────────────────────────────────────────────────────────

def _check_suppression(now: datetime, guard: dict[str, Any], user_timezone: str) -> str | None:
    """Return a suppression_reason string if we should NOT engage, else None."""

    # Quiet hours — checked in the user's own timezone, not UTC
    try:
        local_now = now.astimezone(ZoneInfo(user_timezone))
    except (ZoneInfoNotFoundError, Exception):
        local_now = now  # fall back to UTC if timezone string is invalid
    if local_now.hour >= QUIET_HOUR_START or local_now.hour < QUIET_HOUR_END:
        return "quiet_hours"

    # User is actively using the app right now
    last_interaction = guard.get("last_app_interaction_at")
    if last_interaction:
        try:
            last_dt = datetime.fromisoformat(last_interaction)
            minutes_ago = (now - last_dt).total_seconds() / 60
            if minutes_ago < SUPPRESS_IF_USER_ACTIVE_WITHIN_MINUTES:
                return "user_active_in_app"
        except ValueError:
            pass

    # Time gap since last notification
    last_engaged = guard.get("last_engaged_at")
    if last_engaged:
        try:
            last_dt = datetime.fromisoformat(last_engaged)
            hours_ago = (now - last_dt).total_seconds() / 3600
            if hours_ago < MIN_HOURS_BETWEEN_REACTIVE_NOTIFICATIONS:
                return "too_recent"
        except ValueError:
            pass

    # Daily cap — only proactive (cold) notifications count against this cap;
    # interaction-triggered ones (nutrition scan, calendar) are exempt.
    today = now.date().isoformat()
    if guard.get("guard_date") == today:
        sent_today = guard.get("proactive_notifications_sent_today", 0)
        if sent_today >= MAX_DAILY_PROACTIVE_NOTIFICATIONS:
            return "daily_cap"

    return None


# ── Trigger-specific decision logic ──────────────────────────────────────────

def _decide_nutrition(
    payload: dict[str, Any],
    context: dict[str, Any],
) -> EngagementDecision:
    food_name: str = payload.get("food_name", "").strip().lower()
    verdict: str = payload.get("recommendation", "moderate")   # "eat"|"moderate"|"skip"
    concerns: list[str] = payload.get("concerns", [])

    # Count past scans of the same food (case-insensitive)
    logs: list[dict] = context.get("recent_nutrition_logs", [])
    past_scans = sum(
        1 for log in logs
        if log.get("food_name", "").strip().lower() == food_name
    )

    # Tone selection — fully deterministic
    if verdict == "eat":
        tone = "celebrate"
    elif past_scans == 0:
        tone = "educate"
    elif past_scans < 3:
        tone = "warn"
    else:
        tone = "roast"

    # Skip if there's genuinely nothing worth saying
    if verdict == "moderate" and past_scans == 0 and not concerns:
        return EngagementDecision(
            should_engage=False,
            chosen_agent="none",
            delay_minutes=0,
            tone=tone,
            engagement_context={},
            suppression_reason="nothing_notable",
        )

    engagement_context: dict[str, Any] = {
        "food_name": payload.get("food_name", "Unknown"),
        "first_time": past_scans == 0,
        "past_scan_count": past_scans,
        "concerns": concerns,
        "verdict": verdict,
        "verdict_reason": payload.get("verdict_reason", ""),
        "macros": payload.get("macros", {}),
    }

    profile = context.get("dietary_profile") or {}
    if profile:
        engagement_context["dietary_goal"] = profile.get("goal", "")
        engagement_context["restrictions"] = profile.get("restrictions", [])
        engagement_context["allergies"] = profile.get("allergies", [])

    return EngagementDecision(
        should_engage=True,
        chosen_agent="nutrition_followup",
        delay_minutes=NUTRITION_FOLLOWUP_DELAY_MINUTES,
        tone=tone,
        engagement_context=engagement_context,
    )


def _decide_calendar(
    payload: dict[str, Any],
    context: dict[str, Any],   # reserved for future use (memories, dietary profile)
) -> EngagementDecision:
    _ = context
    event_title: str = payload.get("title", "Meeting")
    minutes_until: int = payload.get("minutes_until", 180)
    description: str = payload.get("description", "")

    # Only send prep notifications for events within the next 3 hours
    if minutes_until > 180:
        return EngagementDecision(
            should_engage=False,
            chosen_agent="none",
            delay_minutes=0,
            tone="check_in",
            engagement_context={},
            suppression_reason="event_too_far",
        )

    return EngagementDecision(
        should_engage=True,
        chosen_agent="calendar_prep",
        delay_minutes=CALENDAR_PREP_DELAY_MINUTES,
        tone="check_in",
        engagement_context={
            "event_title": event_title,
            "minutes_until": minutes_until,
            "description": description,
            "attendees": payload.get("attendees", []),
        },
    )
