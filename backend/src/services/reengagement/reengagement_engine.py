"""Dormancy win-back — one warm "I've been thinking about you" opener for a user who
is ABOUT to fall off the 7-day active-window cliff.

The cliff: ``list_active_user_ids`` only returns users whose FCM token was refreshed
(≈ app launched) within 7 days, so at day 7 of inactivity a user silently stops getting
every proactive notification. This producer catches them at idle day 5-6 — while a push
still reaches them — and sends ONE warm opener through the funnel to pull them back,
fired at most once per dormancy episode.

Piggybacks the scheduler at minute == 45 (offset from thread 0 / briefing 5 / sweep 10 /
icebreaker 15). Like every proactive producer it only ENQUEUES; arbitration, the
tap-gate, the budget, and delivery happen in the drain.
"""

from __future__ import annotations

import asyncio
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...lib.logger import logger
from ..analytics import posthog_client
from ..model_provider import ModelProvider, get_model_provider
from ..notification_service import NotificationResult
from ..notifications import orchestrator
from ..notifications.proposal import SOURCE_REENGAGE, NotificationProposal, ProposalKind
from ..signal_engine.feature_store import list_active_user_ids
from ..signal_engine.scoring import is_within_active_hours
from . import reengagement_store as store

# The idle cohort to win back: registered_at (≈ last app launch) between these many days
# ago. 5-6 days = 1-2 days before the 7-day list_active_user_ids cliff, so a push still
# reaches them. Computed as active(MAX) minus active(MIN).
COHORT_IDLE_MIN_DAYS = 5
COHORT_IDLE_MAX_DAYS = 6
REENGAGE_USER_CONCURRENCY = 10

NOTIFICATION_TYPE_REENGAGE = "reengage"


@dataclass
class ReengageTickSummary:
    cohort: int = 0
    enqueued: int = 0
    skipped_quiet_hours: int = 0
    skipped_claimed: int = 0


_SYSTEM = """\
You are Buddy, a warm AI companion who is genuinely into this person's life. They
haven't opened the app in a few days. Write ONE short push that makes them WANT to come
back — like a close friend who actually missed them. Warm and light, never guilt-trippy,
never salesy, never "we noticed you've been inactive".

Return ONLY JSON:
{"title":"<=40 chars","body":"<=110 chars, warm, second person ('you')","opening_chat_message":"a friendly chat opener to greet them if they tap"}

If a topic they care about is given, you may nod to it naturally; otherwise keep it warm
and general. Output ONLY the JSON."""


def _local_now(timezone_name: str) -> datetime:
    try:
        return datetime.now(ZoneInfo(timezone_name))
    except (ZoneInfoNotFoundError, Exception):
        return datetime.now(UTC)


def _fallback(top_interest: str) -> tuple[str, str, str]:
    if top_interest:
        return (
            "Been thinking of you",
            f"Saw something on {top_interest} and thought of you. Come say hi?",
            f"Hey! It's been a few days. Want to pick back up on {top_interest}?",
        )
    return (
        "Been a minute",
        "It's been a few days and I've missed our chats. Come say hi?",
        "Hey, it's been a little while! How have you been?",
    )


def _parse(raw: str, top_interest: str) -> tuple[str, str, str]:
    cleaned = re.sub(r"^```(?:json)?\s*", "", (raw or "").strip(), flags=re.MULTILINE)
    cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.MULTILINE).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return _fallback(top_interest)
    try:
        data = json.loads(cleaned[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return _fallback(top_interest)
    title = str(data.get("title", "")).strip()[:60]
    body = str(data.get("body", "")).strip()
    opening = str(data.get("opening_chat_message", "")).strip()
    if not title or not body:
        return _fallback(top_interest)
    return title, body, (opening or body)


async def _frame(models: ModelProvider, top_interest: str) -> tuple[str, str, str]:
    """One cheap LLM call for a warm win-back, with a deterministic warm fallback so a
    framer outage never blocks the win-back (the drain's tap-gate is the quality check)."""
    prompt = f"Topic they care about: {top_interest or '(none)'}"
    try:
        raw = await asyncio.wait_for(
            models.cheap(prompt, system=_SYSTEM, temperature=0.7), timeout=8.0
        )
    except Exception as exc:
        logger.warn("reengagement: framer failed, using fallback", {"error": str(exc)})
        return _fallback(top_interest)
    return _parse(str(raw), top_interest)


async def _dormant_cohort() -> set[str]:
    """Users idle COHORT_IDLE_MIN..MAX days = about to hit the 7-day cliff. active(MAX)
    minus active(MIN) is exactly the set whose registered_at is between MIN and MAX days
    ago (the active-user query is registered_at >= now - N days)."""
    active_max = set(await list_active_user_ids(COHORT_IDLE_MAX_DAYS))
    active_min = set(await list_active_user_ids(COHORT_IDLE_MIN_DAYS))
    return active_max - active_min


async def run_reengagement_tick() -> ReengageTickSummary:
    """Public entrypoint, called from the scheduler tick on its minute==45 gate."""
    summary = ReengageTickSummary()
    try:
        cohort = await _dormant_cohort()
    except Exception as exc:
        logger.error("reengagement: failed to compute dormant cohort", {"error": str(exc)})
        return summary

    summary.cohort = len(cohort)
    if not cohort:
        return summary

    models = get_model_provider()
    semaphore = asyncio.Semaphore(REENGAGE_USER_CONCURRENCY)

    async def _with_semaphore(user_id: str) -> None:
        async with semaphore:
            try:
                await _reengage_one(user_id, models, summary)
            except Exception as exc:
                logger.exception("reengagement: per-user failure", {
                    "user_id": user_id, "error": str(exc),
                })

    await asyncio.gather(*[_with_semaphore(uid) for uid in cohort])

    logger.info("reengagement: tick complete", {
        "cohort": summary.cohort,
        "enqueued": summary.enqueued,
        "skipped_quiet_hours": summary.skipped_quiet_hours,
        "skipped_claimed": summary.skipped_claimed,
    })
    # Flush so the win-back funnel events written in on_reengage_delivered (drain) and
    # any enqueue-time events survive the container freeze.
    await posthog_client.flush()
    return summary


async def _reengage_one(
    user_id: str, models: ModelProvider, summary: ReengageTickSummary
) -> None:
    targeting = await store.read_targeting(user_id)

    # Never enqueue during the user's night: a win-back held through quiet hours could
    # expire on the queue TTL before it could send, burning the once-per-episode claim.
    # The cohort window is a full day wide, so a daytime tick will catch them.
    local_now = _local_now(targeting.timezone)
    if not is_within_active_hours(local_now.hour):
        summary.skipped_quiet_hours += 1
        return

    # Once per dormancy episode (atomic, fail-closed).
    if not await store.claim_reengagement(user_id):
        summary.skipped_claimed += 1
        return

    title, body, opening = await _frame(models, targeting.top_interest)
    notification_id = str(uuid.uuid4())

    await orchestrator.submit(
        NotificationProposal(
            user_id=user_id,
            source=SOURCE_REENGAGE,
            kind=ProposalKind.PROACTIVE,
            dedup_key=f"reengage_{user_id}",
            title=title,
            body=body,
            data={
                "deep_link": "chat",
                "notification_id": notification_id,
                "opening_chat_message": opening,
                "notification_origin": "reengage",
            },
            notification_type=NOTIFICATION_TYPE_REENGAGE,
            collapse_key=f"reengage_{user_id}",
        )
    )
    summary.enqueued += 1
    logger.info("reengagement: win-back enqueued", {
        "user_id": user_id, "personalized": bool(targeting.top_interest),
    })


async def on_reengage_delivered(
    proposal: NotificationProposal, result: NotificationResult
) -> None:
    """Post-send hook for a delivered win-back. The once-per-episode claim already
    happened at enqueue (it's the spam guard), so this only logs the real delivery.
    Runs in the drain via post_send.dispatch_post_send; never raises."""
    if not result.delivered:
        return
    logger.info("reengagement: win-back delivered", {"user_id": proposal.user_id})
