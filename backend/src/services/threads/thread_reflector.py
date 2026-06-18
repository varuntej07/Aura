"""The reflector — the slow, restrained heartbeat of the thread engine.

Runs hourly (piggybacked on the existing scheduler tick, no new Cloud Scheduler
job). For each active user it loads their open loops, picks at most ONE worth a
curious follow-up with pure Python, respects quiet hours and a hard daily cap,
then makes one Gemini Flash call to write the question and fires one push.

Most ticks send nothing. That restraint is the feature: a friend who pings you
once in a while about something real feels attentive; one who pings daily gets
muted. Silence on an empty thread set is correct, never a failure.

Selection is split into a pure function (``select_thread_to_follow_up``) so it
is unit-testable without Firestore.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...lib.logger import logger
from ..firebase import admin_firestore
from ..analytics import posthog_client
from ..analytics.funnel_events import (
    EVENT_THREAD_FOLLOWUP_SENT,
    NOTIFICATION_ORIGIN_THREAD_ENGINE,
    PROP_NOTIFICATION_ORIGIN,
    PROP_THREAD_ID,
)
from ..model_provider import ModelProvider, get_model_provider
from ..notification_budget import try_claim_proactive_slot
from ..notification_service import send_notification
from ..signal_engine.feature_store import list_active_user_ids
from ..signal_engine.scoring import is_within_active_hours
from ..user_aura_schema import top_interest_subjects
from . import thread_store
from .models import Thread, ThreadStatus
from .thread_framer import FollowUpFramingContext, frame_follow_up

# ── Tuning constants (single source of truth for the reflector) ─────────────
# Most a single thread is ever followed up on before going dormant. Keeps Buddy
# from nagging about one loop the user clearly is not biting on.
MAX_FOLLOW_UPS_PER_THREAD = 2

# Do not ask about something the user mentioned moments ago — give the loop time
# to actually become an open question worth revisiting.
MIN_THREAD_AGE_BEFORE_FOLLOW_UP = timedelta(hours=1)

# Minimum gap between two follow-ups on the SAME thread.
FOLLOW_UP_COOLDOWN = timedelta(hours=20)

# Hard ceiling on curiosity follow-ups per user per local day across all threads.
# Conservative on purpose; the unified budget (Phase 5) will later coordinate
# this with the signal engine so the two paths share one daily allowance.
THREAD_DAILY_CAP = 1

# Max users processed simultaneously in one tick (mirrors the scoring loop).
REFLECTOR_USER_CONCURRENCY = 10

NOTIFICATION_TYPE_THREAD_FOLLOW_UP = "thread_followup"


@dataclass
class ReflectionSummary:
    users_considered: int = 0
    follow_ups_sent: int = 0
    skipped_quiet_hours: int = 0
    skipped_daily_cap: int = 0
    skipped_no_thread: int = 0


def select_thread_to_follow_up(threads: list[Thread], now: datetime) -> Thread | None:
    """Pick the single most natural open loop to ask about, or None.

    Pure function. Eligibility: still open, under the per-thread follow-up cap,
    old enough to be worth revisiting, and past the per-thread cooldown. Among
    the eligible, prefer the one asked about least, then the one most recently
    referenced by the user (a fresh mention is the most natural to ask about).
    """

    def _eligible(thread: Thread) -> bool:
        if thread.status != ThreadStatus.OPEN:
            return False
        if thread.follow_ups_sent >= MAX_FOLLOW_UPS_PER_THREAD:
            return False
        if thread.created_at is not None and (now - thread.created_at) < MIN_THREAD_AGE_BEFORE_FOLLOW_UP:
            return False
        if thread.last_follow_up_at is not None and (now - thread.last_follow_up_at) < FOLLOW_UP_COOLDOWN:
            return False
        return True

    eligible = [t for t in threads if _eligible(t)]
    if not eligible:
        return None

    # Oldest-touch sentinel keeps threads without a last_touched_at from sorting
    # ahead of genuinely-recent mentions.
    epoch = datetime.min.replace(tzinfo=UTC)
    eligible.sort(
        key=lambda t: (t.follow_ups_sent, -(t.last_touched_at or epoch).timestamp())
    )
    return eligible[0]


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


def _time_band(local_datetime: datetime) -> str:
    h = local_datetime.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 14:
        return "midday"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 22:
        return "evening"
    return "late"


async def _load_user_timezone(user_id: str) -> str:
    def _fetch() -> str:
        doc = admin_firestore().collection("users").document(user_id).get()
        if doc.exists:
            return (doc.to_dict() or {}).get("timezone", "UTC")
        return "UTC"

    try:
        return await asyncio.to_thread(_fetch)
    except Exception:
        return "UTC"


async def _build_framing_context(user_id: str, local_now: datetime) -> FollowUpFramingContext:
    """Read tone, depth, and top interests from UserAura (best-effort)."""

    def _fetch() -> dict[str, Any]:
        snap = admin_firestore().collection("UserAura").document(user_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}

    try:
        aura = await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("threads.thread_reflector: UserAura read failed", {
            "user_id": user_id,
            "error": str(exc),
        })
        aura = {}

    return FollowUpFramingContext(
        dominant_tone=aura.get("dominant_tone"),
        depth_level=int(aura.get("emotional_engagement_level", 1) or 1),
        top_interests=top_interest_subjects(aura, k=3),
        time_band=_time_band(local_now),
    )


async def run_reflection_tick() -> ReflectionSummary:
    """Public entrypoint, called from the scheduler tick on the hourly gate."""
    summary = ReflectionSummary()
    user_ids = await list_active_user_ids()
    summary.users_considered = len(user_ids)
    if not user_ids:
        return summary

    models = get_model_provider()
    semaphore = asyncio.Semaphore(REFLECTOR_USER_CONCURRENCY)

    async def _reflect_with_semaphore(user_id: str) -> None:
        async with semaphore:
            try:
                await _reflect_one_user(user_id, models, summary)
            except Exception as exc:
                logger.exception("threads.thread_reflector: per-user failure", {
                    "user_id": user_id,
                    "error": str(exc),
                })

    await asyncio.gather(*[_reflect_with_semaphore(uid) for uid in user_ids])

    logger.info("threads.thread_reflector: tick complete", {
        "users_considered": summary.users_considered,
        "follow_ups_sent": summary.follow_ups_sent,
        "skipped_quiet_hours": summary.skipped_quiet_hours,
        "skipped_daily_cap": summary.skipped_daily_cap,
        "skipped_no_thread": summary.skipped_no_thread,
    })

    # Drain the funnel queue before Cloud Run freezes the container, or the
    # thread_followup_sent events are silently lost (mirrors the scoring loop).
    await posthog_client.flush()
    return summary


async def _reflect_one_user(
    user_id: str,
    models: ModelProvider,
    summary: ReflectionSummary,
) -> None:
    timezone_name = await _load_user_timezone(user_id)
    local_now = _local_now(timezone_name)
    local_date = local_now.date().isoformat()

    # Never ask a curious question in the middle of the night.
    if not is_within_active_hours(local_now.hour):
        summary.skipped_quiet_hours += 1
        return

    if await thread_store.read_follow_ups_today(user_id, local_date) >= THREAD_DAILY_CAP:
        summary.skipped_daily_cap += 1
        return

    threads = await thread_store.list_open_threads(user_id)
    chosen = select_thread_to_follow_up(threads, datetime.now(UTC))
    if chosen is None:
        summary.skipped_no_thread += 1
        return

    # Coordinated ceiling shared with the signal engine + engagement (no-op
    # while the flag is off). The per-day THREAD_DAILY_CAP above is the per-source
    # sub-limit; this is the global one.
    budget = await try_claim_proactive_slot(
        user_id, source="thread_engine", user_local_date=local_date,
    )
    if not budget.allowed:
        summary.skipped_daily_cap += 1
        return

    ctx = await _build_framing_context(user_id, local_now)
    framed = await frame_follow_up(models, chosen, ctx)

    sent_at = datetime.now(UTC)
    result = await send_notification(
        user_id,
        title=framed.title,
        body=framed.body,
        data={
            "deep_link": "chat",
            "thread_id": chosen.thread_id,
            "question": framed.body,
            # FCM data values must be strings — the client JSON-decodes this into
            # the chips it renders (Android) or the in-chat pills (iOS).
            "suggested_replies": json.dumps(framed.suggested_replies),
            "opening_chat_message": framed.body,
            "notification_origin": NOTIFICATION_ORIGIN_THREAD_ENGINE,
        },
        notification_type=NOTIFICATION_TYPE_THREAD_FOLLOW_UP,
        collapse_key=f"thread_{chosen.thread_id}",
        # Android builds the chip notification from the data payload; iOS still
        # gets an alert via APNS and renders the chips in-chat on tap.
        data_only=True,
    )

    if not result.delivered:
        logger.info("threads.thread_reflector: send returned no delivery", {
            "user_id": user_id,
            "thread_id": chosen.thread_id,
        })
        return

    await thread_store.mark_follow_up_sent(user_id, chosen.thread_id, sent_at)
    await thread_store.record_follow_up_in_budget(user_id, local_date, sent_at)
    summary.follow_ups_sent += 1

    # Top of the thread funnel. Fire-and-forget; never blocks the tick. The
    # join key + origin must match the client's tap/session/reply events so
    # PostHog can join sent -> tapped -> session -> reply.
    await posthog_client.capture_event(
        distinct_id=user_id,
        event=EVENT_THREAD_FOLLOWUP_SENT,
        properties={
            PROP_THREAD_ID: chosen.thread_id,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_THREAD_ENGINE,
            "source": str(chosen.source),
        },
    )

    # Retire a thread that has now exhausted its follow-up budget so it is never
    # picked again — the user clearly is not biting. A later user reply moves it
    # to ENGAGED instead (handled on the reply-ingest path).
    if chosen.follow_ups_sent + 1 >= MAX_FOLLOW_UPS_PER_THREAD:
        await thread_store.set_status(user_id, chosen.thread_id, ThreadStatus.DORMANT)

    logger.info("threads.thread_reflector: follow-up sent", {
        "user_id": user_id,
        "thread_id": chosen.thread_id,
        "source": str(chosen.source),
    })
