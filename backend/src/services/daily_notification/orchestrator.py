"""
Calendar reminder orchestrator — the calendar notification pipeline per user.

Called once per user per day, triggered by Cloud Tasks at 7 AM local time
(the fan-out handler in daily_notification.py schedules these).

Pipeline:
  1.  Idempotency check — skip if daily_plans/{today} already exists
  2.  Load user timezone
  3.  Fetch upcoming calendar events and recent queries in parallel
  4.  CalendarNotificationAgent classifies today's and 3-day-ahead events
  5.  Compute meeting reminder send times, enforce global 2-hour gap constraint
  6.  Write daily_plans/{today} to Firestore (meeting reminders only)
  7.  Schedule Cloud Tasks for meeting reminders
  8.  Generate daily home-screen suggestion pills
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
from .models import CalendarNotificationContent, MeetingReminderPlan
from .calendar_notification_agent import CalendarNotificationAgent
from .suggestion_pills_agent import SuggestionPillsAgent

MAX_DAILY_MEETING_REMINDERS = 3
_THREE_HOURS = timedelta(hours=3)
_TWO_HOURS = timedelta(hours=2)

# Module-level agent singletons
_models: ModelProvider | None = None
_suggestion_pills: SuggestionPillsAgent | None = None
_calendar_agent: CalendarNotificationAgent | None = None


def _get_agents() -> tuple[SuggestionPillsAgent, CalendarNotificationAgent]:
    global _models, _suggestion_pills, _calendar_agent
    if _models is None:
        _models = ModelProvider()
    if _suggestion_pills is None:
        _suggestion_pills = SuggestionPillsAgent(_models)
    if _calendar_agent is None:
        _calendar_agent = CalendarNotificationAgent(_models)
    return _suggestion_pills, _calendar_agent


# Public entry point
async def run_daily_plan(user_id: str) -> None:
    """Run the calendar reminder pipeline for a user. Never raises — errors are logged."""
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

    # Step 2: Load timezone
    user_timezone = await _load_user_timezone(user_id)

    # Step 3: Fetch calendar events and recent queries in parallel
    upcoming_events, queries = await asyncio.gather(
        _fetch_upcoming_calendar_events(user_id, days_ahead=7),
        _fetch_last_10_queries(user_id),
    )

    pills_agent, cal_agent = _get_agents()

    # Step 4: CalendarNotificationAgent classifies events and generates reminder content
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

    # Step 5: Write daily_plans/{today} with meeting reminders
    await _write_calendar_plan(user_id, today, meeting_reminders)

    # Step 6: Schedule Cloud Tasks for meeting reminders
    reminder_tasks = await asyncio.gather(
        *[
            _schedule_nudge_send(user_id, today, f"meeting_reminder_{i}", r.send_at_utc)
            for i, r in enumerate(meeting_reminders)
        ]
    )

    tasks_ok = all(reminder_tasks) if reminder_tasks else True
    logger.info("daily_notification: calendar plan complete", {
        "user_id": user_id,
        "date": today,
        "meeting_reminders_scheduled": len(meeting_reminders),
        "tasks_scheduled": tasks_ok,
    })

    # Step 7: Generate daily home-screen suggestion pills. Failure must not affect reminders.
    try:
        await pills_agent.generate_all_agent_suggestion_pills(user_id, queries)
    except Exception as exc:
        logger.exception("daily_notification: suggestion pills generation failed", {
            "user_id": user_id,
            "error": str(exc),
        })


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


async def _write_calendar_plan(
    user_id: str,
    plan_date: str,
    meeting_reminders: list[MeetingReminderPlan],
) -> None:
    def _write() -> None:
        db = admin_firestore()
        doc: dict[str, Any] = {
            "plan_date": plan_date,
            "plan_source": "calendar_only",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        for i, reminder in enumerate(meeting_reminders):
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
        logger.exception("daily_notification: failed to write calendar_plan", {
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

        try:
            send_dt = datetime.fromisoformat(send_at_utc)
            if send_dt.tzinfo is None:
                send_dt = send_dt.replace(tzinfo=timezone.utc)
        except ValueError:
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
        await asyncio.to_thread(
            lambda: admin_firestore()
            .collection("users").document(user_id)
            .collection("daily_plans").document(plan_date)
            .update({f"{nudge_slot}.cloud_task_name": task_name})
        )
        logger.info("daily_notification: reminder task scheduled", {
            "user_id": user_id,
            "nudge_slot": nudge_slot,
            "send_at_utc": send_at_utc,
            "task_name": task_name,
        })
        return True
    except Exception as exc:
        logger.exception("daily_notification: failed to schedule reminder task", {
            "user_id": user_id,
            "nudge_slot": nudge_slot,
            "error": str(exc),
        })
        return False


# Calendar event helpers

async def _fetch_upcoming_calendar_events(user_id: str, days_ahead: int = 7) -> list[dict]:
    """Read cached calendar events from Firestore for the next N days.

    Does not trigger a live sync — relies on the scheduler tick keeping
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

    return [plan for _, plan in kept[:MAX_DAILY_MEETING_REMINDERS]]
