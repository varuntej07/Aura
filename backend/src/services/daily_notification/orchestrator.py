"""
Daily notification orchestrator — the full planning pipeline per user.

Called once per user per day, triggered by Cloud Tasks at 7 AM local time
(the fan-out handler in daily_notification.py schedules these).

Pipeline:
  1.  Idempotency check — skip if daily_plans/{today} already exists
  2.  Load user timezone + check daily cap
  3.  Fetch context in parallel (queries, dietary profile, recent plans, calendar events)
  4.  Fetch RSS news headlines
  5.  NotificationPlannerAgent generates DailyPlan (calendar-aware)
  6.  PushNotificationAgent verifies the plan (Stage 1: hard rules, Stage 2: LLM)
  7.  If rejected → retry planner ONCE with feedback injected
  8.  If still rejected → use safe_default plan (never skips a day)
  9.  CalendarNotificationAgent classifies today's and 3-day-ahead events
  10. Compute meeting reminder send times, enforce global 2-hour gap constraint
  11. Write daily_plans/{today} to Firestore (nudges + meeting reminders)
  12. Schedule Cloud Tasks for nudges and meeting reminders
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timezone, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from langsmith import traceable

from ...lib.logger import logger
from ...services.firebase import admin_firestore
from ...services.model_provider import ModelProvider
from . import rss_client
from .models import CalendarNotificationContent, DailyPlan, MeetingReminderPlan, NudgePlan
from .calendar_notification_agent import CalendarNotificationAgent
from .planner_agent import NotificationPlannerAgent
from .suggestion_pills_agent import SuggestionPillsAgent
from .verifier_agent import PushNotificationAgent

MAX_DAILY_MEETING_REMINDERS = 3
_THREE_HOURS = timedelta(hours=3)
_TWO_HOURS = timedelta(hours=2)

# Module-level agent singletons
_models: ModelProvider | None = None
_planner: NotificationPlannerAgent | None = None
_verifier: PushNotificationAgent | None = None
_suggestion_pills: SuggestionPillsAgent | None = None
_calendar_agent: CalendarNotificationAgent | None = None


def _get_agents() -> tuple[
    NotificationPlannerAgent,
    PushNotificationAgent,
    SuggestionPillsAgent,
    CalendarNotificationAgent,
]:
    global _models, _planner, _verifier, _suggestion_pills, _calendar_agent
    if _models is None:
        _models = ModelProvider()
    if _planner is None:
        _planner = NotificationPlannerAgent(_models)
    if _verifier is None:
        _verifier = PushNotificationAgent(_models)
    if _suggestion_pills is None:
        _suggestion_pills = SuggestionPillsAgent(_models)
    if _calendar_agent is None:
        _calendar_agent = CalendarNotificationAgent(_models)
    return _planner, _verifier, _suggestion_pills, _calendar_agent


# Public entry point 
async def run_daily_plan(user_id: str) -> None:
    """Plan today's two notifications for a user. Never raises — errors are logged."""
    try:
        await _run(user_id)
    except Exception as exc:
        logger.exception("daily_notification.orchestrator: unhandled error", {
            "user_id": user_id,
            "error": str(exc),
        })


# Core pipeline
@traceable(name="daily_notification_plan", run_type="chain")
async def _run(user_id: str) -> None:
    today = date.today().isoformat()

    # Step 1: Idempotency — skip if plan already exists for today
    if await _daily_plan_exists(user_id, today):
        logger.info("daily_notification: plan already exists, skipping", {
            "user_id": user_id,
            "date": today,
        })
        return

    # Step 2: Load timezone and check daily cap
    user_timezone = await _load_user_timezone(user_id)
    if await _daily_cap_reached(user_id, today):
        logger.info("daily_notification: daily cap already reached, skipping", {
            "user_id": user_id,
            "date": today,
        })
        return

    # Step 3: Fetch context in parallel (calendar events included)
    queries, dietary_profile, recent_plans, upcoming_events = await asyncio.gather(
        _fetch_last_10_queries(user_id),
        _fetch_dietary_profile(user_id),
        _fetch_last_2_daily_plans(user_id),
        _fetch_upcoming_calendar_events(user_id, days_ahead=7),
    )

    topics_sent_yesterday = _extract_topics_from_plans(recent_plans)
    topic_keywords = _extract_topic_keywords(queries)

    # Step 4: Fetch RSS news (used as fallback or enrichment context for the planner)
    news_items = await rss_client.fetch_news(topic_keywords)

    context: dict[str, Any] = {
        "recent_queries": queries,
        "dietary_profile": dietary_profile,
        "topics_sent_yesterday": topics_sent_yesterday,
        "news_items": news_items,
        "upcoming_events": upcoming_events,
        "user_timezone": user_timezone,
        "current_local_datetime": _local_now_iso(user_timezone),
        "retry_feedback": None,
    }

    planner, verifier, pills_agent, cal_agent = _get_agents()

    retry_count = 0
    rejection_feedback: str | None = None

    # Steps 5–6: Generate first plan and verify.
    # The planner can raise (truncated JSON from token ceiling, timeout, parse failure).
    # Treat any exception as a failed plan so the retry and safe_default paths still fire but a missed notification is never acceptable.
    plan = None
    result = None
    try:
        plan = await planner.generate(context)
        result = await verifier.verify(plan, topics_sent_yesterday, dietary_profile)
    except Exception as exc:
        logger.warn("daily_notification: planner raised on first attempt", {
            "user_id": user_id,
            "error_type": type(exc).__name__,
            "error": str(exc),
        })

    # Step 7: One retry if the plan was rejected or the planner failed to produce one.
    if plan is None or (result is not None and not result.approved):
        if result is not None and not result.approved:
            rejection_feedback = result.feedback_for_planner
            logger.info("daily_notification: plan rejected, retrying once", {
                "user_id": user_id,
                "rejection_reason": result.rejection_reason,
                "feedback": result.feedback_for_planner,
            })
            context["retry_feedback"] = result.feedback_for_planner
        retry_count = 1
        plan = None
        result = None
        try:
            plan = await planner.generate(context)
            result = await verifier.verify(plan, topics_sent_yesterday, dietary_profile)
        except Exception as exc:
            logger.warn("daily_notification: planner raised on retry", {
                "user_id": user_id,
                "error_type": type(exc).__name__,
                "error": str(exc),
            })

    # Step 8: Safe default if both attempts failed or were rejected.
    if plan is None or (result is not None and not result.approved):
        if result is not None and not result.approved:
            logger.warn("daily_notification: retry also rejected, using safe default", {
                "user_id": user_id,
                "rejection_reason": result.rejection_reason,
            })
        plan = _make_safe_default_plan(news_items, user_timezone)

    # Step 9: CalendarNotificationAgent classifies events and generates reminder content.
    # Runs after the daily plan is finalized so reminder scheduling doesn't affect plan quality.
    events_today, events_three_days_away = _partition_events_for_notification_planning(
        upcoming_events, user_timezone
    )
    meeting_reminders: list[MeetingReminderPlan] = []
    try:
        cal_batch = await cal_agent.generate_reminders(
            events_today=events_today,
            events_three_days_away=events_three_days_away,
            user_timezone=user_timezone,
        )
        now_utc = datetime.now(timezone.utc)
        meeting_reminders = _build_meeting_reminder_plans(
            cal_batch.reminders, upcoming_events, now_utc, user_timezone
        )
    except Exception as exc:
        logger.warn("daily_notification: calendar agent failed, no meeting reminders today", {
            "user_id": user_id,
            "error": str(exc),
        })

    # Step 10: Compute send times, enforce global 2-hour gap, cap reminders.
    # Always recompute nudge UTC from the validated local time — never trust LLM-generated UTC.
    morning_utc = _local_hhmm_to_utc(plan.morning_nudge.send_at_local_time, user_timezone)
    evening_utc = _local_hhmm_to_utc(plan.evening_nudge.send_at_local_time, user_timezone)

    reminder_send_times = [r.send_at_utc for r in meeting_reminders]
    morning_utc, evening_utc = _apply_two_hour_gap_to_nudges(
        morning_utc, evening_utc, reminder_send_times, user_timezone
    )

    # Step 11: Write daily_plans/{today} with nudges and meeting reminders.
    await _write_daily_plan(user_id, today, plan, retry_count, rejection_feedback,
                            morning_utc_override=morning_utc,
                            evening_utc_override=evening_utc,
                            meeting_reminders=meeting_reminders)

    # Step 12: Schedule Cloud Tasks for nudges and meeting reminders.
    nudge_tasks = await asyncio.gather(
        _schedule_nudge_send(user_id, today, "morning_nudge", morning_utc),
        _schedule_nudge_send(user_id, today, "evening_nudge", evening_utc),
    )
    reminder_tasks = await asyncio.gather(
        *[
            _schedule_nudge_send(user_id, today, f"meeting_reminder_{i}", r.send_at_utc)
            for i, r in enumerate(meeting_reminders)
        ]
    )

    tasks_ok = all(nudge_tasks) and all(reminder_tasks)
    logger.info("daily_notification: plan complete", {
        "user_id": user_id,
        "date": today,
        "plan_source": plan.plan_source,
        "morning_topic": plan.morning_nudge.topic,
        "evening_topic": plan.evening_nudge.topic,
        "meeting_reminders_scheduled": len(meeting_reminders),
        "retry_count": retry_count,
        "tasks_scheduled": tasks_ok,
    })
    if not tasks_ok:
        logger.error("daily_notification: one or more tasks failed to schedule", {
            "user_id": user_id,
            "date": today,
        })

    # Generate daily home-screen suggestion pills after the core notification
    # pipeline is durable. Failure here must not affect scheduled nudges.
    try:
        await pills_agent.generate_all_agent_suggestion_pills(user_id, queries)
    except Exception as exc:
        logger.exception("daily_notification: suggestion pills generation failed", {
            "user_id": user_id,
            "error": str(exc),
        })


# Safe default plan 
def _make_safe_default_plan(news_items: list[dict], user_timezone: str) -> DailyPlan:
    """Always-valid fallback. Uses the top news headline if available."""
    top_news_title = news_items[0]["title"] if news_items else "New research on health and habits"
    # Truncate to fit notification title limit
    news_title_short = (top_news_title[:47] + "...") if len(top_news_title) > 50 else top_news_title

    morning_utc = _local_hhmm_to_utc("08:30", user_timezone)
    evening_utc = _local_hhmm_to_utc("19:00", user_timezone)

    return DailyPlan(
        morning_nudge=NudgePlan(
            topic="news",
            title=news_title_short,
            body="Something worth knowing today — tap to read more.",
            send_at_local_time="08:30",
            send_at_utc=morning_utc,
            why_this_topic="Safe default: top health news headline",
            opening_chat_message=f"I came across something interesting: {top_news_title}. Thought it might be relevant to your goals.",
            quick_reply_chips=["Tell me more", "Not interested", "What else is new?"],
        ),
        evening_nudge=NudgePlan(
            topic="habit",
            title="How'd today go?",
            body="Quick check-in — let's see how the day treated you.",
            send_at_local_time="19:00",
            send_at_utc=evening_utc,
            why_this_topic="Safe default: evening wellness check-in",
            opening_chat_message="Just checking in — how did today go? Anything you want to talk through or track?",
            quick_reply_chips=["It went well!", "Could've been better", "Skip for now"],
        ),
        plan_source="safe_default",
    )


# Firestore helpers 
async def _daily_plan_exists(user_id: str, plan_date: str) -> bool:
    def _check() -> bool:
        db = admin_firestore()
        doc = (
            db.collection("users").document(user_id)
            .collection("daily_plans").document(plan_date)
            .get()
        )
        return doc.exists
    try:
        return await asyncio.to_thread(_check)
    except Exception:
        return False


async def _daily_cap_reached(user_id: str, today: str) -> bool:
    """Returns True if the user has already received their daily notification quota."""
    from ...services.engagement.decision_engine import MAX_DAILY_PROACTIVE_NOTIFICATIONS
    def _check() -> bool:
        db = admin_firestore()
        doc = (
            db.collection("users").document(user_id)
            .collection("engagement_guard").document("state")
            .get()
        )
        if not doc.exists:
            return False
        guard = doc.to_dict() or {}
        if guard.get("guard_date") != today:
            return False
        sent_today = guard.get("proactive_notifications_sent_today", 0)
        return sent_today >= MAX_DAILY_PROACTIVE_NOTIFICATIONS
    try:
        return await asyncio.to_thread(_check)
    except Exception:
        return False


async def _load_user_timezone(user_id: str) -> str:
    def _fetch() -> str:
        db = admin_firestore()
        doc = db.collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone", "UTC")
        return "UTC"
    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return "UTC"


async def _fetch_last_10_queries(user_id: str) -> list[dict]:
    def _fetch() -> list[dict]:
        db = admin_firestore()
        docs = (
            db.collection("users").document(user_id)
            .collection("queries")
            .order_by("timestamp", direction="DESCENDING")
            .limit(10)
            .stream()
        )
        return [{"id": d.id, **d.to_dict()} for d in docs]
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("daily_notification: queries fetch failed", {"error": str(exc)})
        return []


async def _fetch_dietary_profile(user_id: str) -> dict:
    def _fetch() -> dict:
        db = admin_firestore()
        doc = (
            db.collection("users").document(user_id)
            .collection("dietary_profile").document("data")
            .get()
        )
        return doc.to_dict() if doc.exists else {}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("daily_notification: dietary_profile fetch failed", {"error": str(exc)})
        return {}


async def _fetch_last_2_daily_plans(user_id: str) -> list[dict]:
    def _fetch() -> list[dict]:
        db = admin_firestore()
        docs = (
            db.collection("users").document(user_id)
            .collection("daily_plans")
            .order_by("plan_date", direction="DESCENDING")
            .limit(2)
            .stream()
        )
        return [d.to_dict() for d in docs]
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("daily_notification: daily_plans fetch failed", {"error": str(exc)})
        return []


async def _write_daily_plan(
    user_id: str,
    plan_date: str,
    plan: DailyPlan,
    retry_count: int,
    rejection_feedback: str | None,
    morning_utc_override: str | None = None,
    evening_utc_override: str | None = None,
    meeting_reminders: list[MeetingReminderPlan] | None = None,
) -> None:
    def _write() -> None:
        db = admin_firestore()
        morning_data = plan.morning_nudge.model_dump()
        if morning_utc_override:
            morning_data["send_at_utc"] = morning_utc_override
        evening_data = plan.evening_nudge.model_dump()
        if evening_utc_override:
            evening_data["send_at_utc"] = evening_utc_override

        doc: dict[str, Any] = {
            "plan_date": plan_date,
            "plan_source": plan.plan_source,
            "morning_nudge": {**morning_data, "status": "scheduled", "cloud_task_name": None, "sent_at": None},
            "evening_nudge": {**evening_data, "status": "scheduled", "cloud_task_name": None, "sent_at": None},
            "rejection_feedback": rejection_feedback,
            "retry_count": retry_count,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        for i, reminder in enumerate(meeting_reminders or []):
            doc[f"meeting_reminder_{i}"] = {
                **reminder.model_dump(),
                "status": "scheduled",
                "cloud_task_name": None,
                "sent_at": None,
            }

        db.collection("users").document(user_id)\
            .collection("daily_plans").document(plan_date).set(doc)
    try:
        await asyncio.to_thread(_write)
    except Exception as exc:
        logger.exception("daily_notification: failed to write daily_plan", {
            "user_id": user_id,
            "error": str(exc),
        })
        raise


async def _schedule_nudge_send(
    user_id: str,
    plan_date: str,
    nudge_slot: str,
    send_at_utc: str,
) -> bool:
    """Schedule a Cloud Task to fire at send_at_utc → POST /internal/daily-notify/send."""
    from ...config.settings import settings

    def _enqueue() -> str:
        from google.cloud import tasks_v2  # type: ignore
        from google.protobuf import timestamp_pb2  # type: ignore

        client = tasks_v2.CloudTasksClient()
        queue_path = client.queue_path(
            settings.CLOUD_TASKS_PROJECT,
            settings.CLOUD_TASKS_LOCATION,
            settings.CLOUD_TASKS_QUEUE,
        )

        payload = {
            "user_id": user_id,
            "plan_date": plan_date,
            "nudge_slot": nudge_slot,
        }

        # Parse send_at_utc to a Unix timestamp for Cloud Tasks scheduling
        try:
            send_dt = datetime.fromisoformat(send_at_utc)
            if send_dt.tzinfo is None:
                send_dt = send_dt.replace(tzinfo=timezone.utc)
        except ValueError:
            # If the datetime is malformed, send in 1 hour as a safe fallback
            send_dt = datetime.now(timezone.utc) + timedelta(hours=1)

        eta = timestamp_pb2.Timestamp()
        eta.FromSeconds(int(send_dt.timestamp()))

        task: dict[str, Any] = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{settings.BACKEND_INTERNAL_URL}/internal/daily-notify/send",
                "headers": {"Content-Type": "application/json"},
                "body": json.dumps(payload).encode(),
                "oidc_token": {
                    "service_account_email": settings.SCHEDULER_SA_EMAIL,
                    "audience": settings.BACKEND_INTERNAL_URL,
                },
            },
            "schedule_time": eta,
        }

        created = client.create_task(parent=queue_path, task=task)
        return created.name

    try:
        task_name = await asyncio.to_thread(_enqueue)
        # Store the task name in daily_plans so it can be cancelled if needed
        await asyncio.to_thread(
            lambda: admin_firestore()
            .collection("users").document(user_id)
            .collection("daily_plans").document(plan_date)
            .update({f"{nudge_slot}.cloud_task_name": task_name})
        )
        logger.info("daily_notification: nudge task scheduled", {
            "user_id": user_id,
            "nudge_slot": nudge_slot,
            "send_at_utc": send_at_utc,
            "task_name": task_name,
        })
        return True
    except Exception as exc:
        logger.exception("daily_notification: failed to schedule nudge task", {
            "user_id": user_id,
            "nudge_slot": nudge_slot,
            "error": str(exc),
        })
        return False


# Context extraction helpers 
def _extract_topic_keywords(queries: list[dict]) -> list[str]:
    """Extract topic keywords from recent queries for RSS search."""
    keywords: set[str] = set()
    topic_word_map = {
        "nutrition": ["nutrition", "food", "eat", "diet", "calories", "protein", "carbs", "fat", "meal"],
        "workout": ["workout", "gym", "exercise", "run", "lift", "training", "cardio", "weights"],
        "sleep": ["sleep", "insomnia", "tired", "rest", "bedtime", "fatigue"],
        "hydration": ["water", "hydrat", "thirst"],
        "mindfulness": ["stress", "anxiety", "meditat", "mindful", "mental"],
    }
    for query in queries:
        text = query.get("text", "").lower()
        for topic, words in topic_word_map.items():
            if any(w in text for w in words):
                keywords.add(topic)
    return list(keywords) if keywords else []


def _extract_topics_from_plans(recent_plans: list[dict]) -> list[str]:
    """Extract topic names from the last 2 daily plans to avoid repetition."""
    topics: list[str] = []
    for plan in recent_plans:
        for slot in ("morning_nudge", "evening_nudge"):
            nudge = plan.get(slot, {})
            topic = nudge.get("topic")
            if topic and topic not in topics:
                topics.append(topic)
    return topics


# Timezone helpers 
def _local_now_iso(user_timezone: str) -> str:
    """Return the current datetime in the user's timezone as an ISO string."""
    try:
        tz = ZoneInfo(user_timezone)
        return datetime.now(tz).isoformat()
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(timezone.utc).isoformat()


def _local_hhmm_to_utc(hhmm: str, user_timezone: str) -> str:
    """Convert today's "HH:MM" in user_timezone to a UTC ISO datetime string.

    Never rolls to tomorrow: Cloud Tasks fires past-scheduled tasks immediately,
    which is the correct behaviour for late-running notifications. Rolling to
    tomorrow would silently skip an entire day of nudges.
    """
    try:
        tz = ZoneInfo(user_timezone)
        h, m = int(hhmm.split(":")[0]), int(hhmm.split(":")[1])
        local_now = datetime.now(tz)
        local_target = local_now.replace(hour=h, minute=m, second=0, microsecond=0)
        return local_target.astimezone(timezone.utc).isoformat()
    except Exception:
        # Fallback: UTC now + 1 hour
        return (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()


# Calendar event helpers

async def _fetch_upcoming_calendar_events(user_id: str, days_ahead: int = 7) -> list[dict]:
    """Read cached calendar events from Firestore for the next N days.

    Does not trigger a live sync -- relies on the scheduler tick keeping
    calendar_events fresh via GoogleCalendarConnector.process_pending_sync_jobs.
    Returns an empty list if the calendar integration is not connected.
    """
    def _fetch() -> list[dict]:
        db = admin_firestore()

        integration = (
            db.collection("users").document(user_id)
            .collection("integrations").document("google_calendar")
            .get()
        )
        if not integration.exists or not (integration.to_dict() or {}).get("enabled"):
            return []

        now_utc = datetime.now(timezone.utc)
        end_utc = now_utc + timedelta(days=days_ahead)

        snapshot = (
            db.collection("users").document(user_id)
            .collection("calendar_events")
            .where("start_at_ts", ">=", now_utc)
            .where("start_at_ts", "<", end_utc)
            .order_by("start_at_ts")
            .limit(50)
            .stream()
        )

        events: list[dict] = []
        for doc in snapshot:
            data = doc.to_dict() or {}
            if str(data.get("status", "")).lower() == "cancelled":
                continue
            if data.get("is_all_day"):
                continue
            events.append({
                "id": data.get("provider_event_id") or doc.id,
                "title": data.get("summary") or "",
                "description": (data.get("description") or "")[:200],
                "start_at": data.get("start_at") or "",
                "end_at": data.get("end_at") or "",
                "attendee_count": len(data.get("attendees") or []),
            })
        return events

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("daily_notification: calendar events fetch failed", {"error": str(exc)})
        return []


def _partition_events_for_notification_planning(
    events: list[dict],
    user_timezone: str,
) -> tuple[list[dict], list[dict]]:
    """Split events into two buckets for the CalendarNotificationAgent.

    Returns:
        events_today: events whose local date equals today (three_hour_before candidates)
        events_three_days_away: events whose local date is exactly 3 days from today
                                (three_day_ahead candidates for high-importance events)
    """
    try:
        tz = ZoneInfo(user_timezone)
    except Exception:
        tz = ZoneInfo("UTC")

    today_local = datetime.now(tz).date()
    three_days_from_today = today_local + timedelta(days=3)

    events_today: list[dict] = []
    events_three_days_away: list[dict] = []

    for event in events:
        start_at = event.get("start_at")
        if not start_at:
            continue
        try:
            start_dt = datetime.fromisoformat(start_at.replace("Z", "+00:00")).astimezone(tz)
            event_date = start_dt.date()
            if event_date == today_local:
                events_today.append(event)
            elif event_date == three_days_from_today:
                events_three_days_away.append(event)
        except Exception:
            continue

    return events_today, events_three_days_away


def _build_meeting_reminder_plans(
    contents: list[CalendarNotificationContent],
    all_events: list[dict],
    now_utc: datetime,
    user_timezone: str,
) -> list[MeetingReminderPlan]:
    """Compute send_at_utc for each CalendarNotificationContent and assemble MeetingReminderPlans.

    Timing rules:
      three_hour_before: send at event_start - 3 hours. Skip if that time is already past
                         or if the event starts in less than 3 hours (too late).
      three_day_ahead:   send at now + 3 hours (fires ~10 AM on planning day).

    After computing times, enforces the 2-hour gap between reminders by dropping the
    lower-priority reminder when two fall within 2 hours of each other.
    Caps the final list at MAX_DAILY_MEETING_REMINDERS.
    """
    event_start_map: dict[str, datetime] = {}
    for event in all_events:
        start_at = event.get("start_at")
        event_id = event.get("id", "")
        if start_at and event_id:
            try:
                event_start_map[event_id] = datetime.fromisoformat(
                    start_at.replace("Z", "+00:00")
                ).astimezone(timezone.utc)
            except Exception:
                pass

    candidates: list[tuple[datetime, MeetingReminderPlan]] = []

    for content in contents:
        try:
            if content.notification_type == "three_hour_before":
                event_start = event_start_map.get(content.event_id)
                if event_start is None:
                    continue
                send_at = event_start - _THREE_HOURS
                if send_at <= now_utc:
                    logger.info(
                        "daily_notification: skipping meeting reminder, window passed",
                        {"event_id": content.event_id, "send_at": send_at.isoformat()},
                    )
                    continue
            else:
                # three_day_ahead fires 3 hours after the 7 AM planning run (~10 AM local)
                send_at = now_utc + _THREE_HOURS

            plan = MeetingReminderPlan(
                event_id=content.event_id,
                event_title=content.event_title,
                importance_tier=content.importance_tier,
                notification_type=content.notification_type,
                title=content.title,
                body=content.body,
                send_at_utc=send_at.isoformat(),
                opening_chat_message=content.opening_chat_message,
                quick_reply_chips=content.quick_reply_chips,
                why_this_notification=content.why_this_notification,
            )
            candidates.append((send_at, plan))
        except Exception as exc:
            logger.warn("daily_notification: failed to build meeting reminder plan", {
                "event_id": content.event_id,
                "error": str(exc),
            })

    # Sort by send time, then enforce 2-hour gap between reminders.
    # When two reminders are within 2 hours of each other, drop the medium-importance one.
    # If same tier, drop the later one.
    candidates.sort(key=lambda x: x[0])
    kept: list[tuple[datetime, MeetingReminderPlan]] = []
    for send_at, plan in candidates:
        too_close = any(
            abs((send_at - kept_time).total_seconds()) < _TWO_HOURS.total_seconds()
            for kept_time, _ in kept
        )
        if not too_close:
            kept.append((send_at, plan))
        else:
            logger.info("daily_notification: dropping meeting reminder due to 2h gap constraint", {
                "event_id": plan.event_id,
                "send_at": send_at.isoformat(),
            })

    result = [plan for _, plan in kept[:MAX_DAILY_MEETING_REMINDERS]]
    return result


def _apply_two_hour_gap_to_nudges(
    morning_utc: str,
    evening_utc: str,
    reminder_send_times: list[str],
    user_timezone: str,
) -> tuple[str, str]:
    """Shift morning and evening nudge times to maintain a 2-hour gap from meeting reminders.

    Meeting reminder times are fixed. Nudges shift within their allowed windows
    (morning: 08:00-12:00, evening: 17:00-21:00) to avoid conflicts.
    If a nudge cannot fit without conflict, it stays at the window boundary closest
    to its original time and a warning is logged.
    """
    if not reminder_send_times:
        return morning_utc, evening_utc

    MORNING_WINDOW_START_H = 8
    MORNING_WINDOW_END_H = 12
    EVENING_WINDOW_START_H = 17
    EVENING_WINDOW_END_H = 21

    try:
        tz = ZoneInfo(user_timezone)
    except Exception:
        tz = ZoneInfo("UTC")

    def parse_dt(s: str) -> datetime:
        dt = datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)

    def clamp_to_window(dt: datetime, start_h: int, end_h: int) -> datetime:
        local = dt.astimezone(tz)
        window_start = local.replace(hour=start_h, minute=0, second=0, microsecond=0)
        window_end = local.replace(hour=end_h, minute=0, second=0, microsecond=0)
        clamped_local = max(window_start, min(window_end, local))
        return clamped_local.astimezone(timezone.utc)

    reminder_dts = sorted([parse_dt(s) for s in reminder_send_times])
    morning_dt = parse_dt(morning_utc)
    evening_dt = parse_dt(evening_utc)

    for reminder_dt in reminder_dts:
        gap_seconds = (morning_dt - reminder_dt).total_seconds()
        if abs(gap_seconds) < _TWO_HOURS.total_seconds():
            if gap_seconds >= 0:
                morning_dt = reminder_dt + _TWO_HOURS
            else:
                morning_dt = reminder_dt - _TWO_HOURS
            morning_dt = clamp_to_window(morning_dt, MORNING_WINDOW_START_H, MORNING_WINDOW_END_H)

    for reminder_dt in reminder_dts:
        gap_seconds = (evening_dt - reminder_dt).total_seconds()
        if abs(gap_seconds) < _TWO_HOURS.total_seconds():
            if gap_seconds >= 0:
                evening_dt = reminder_dt + _TWO_HOURS
            else:
                evening_dt = reminder_dt - _TWO_HOURS
            evening_dt = clamp_to_window(evening_dt, EVENING_WINDOW_START_H, EVENING_WINDOW_END_H)

    return morning_dt.isoformat(), evening_dt.isoformat()
