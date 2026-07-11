"""Thread-engine helpers the Curiosity agent reuses or hands off to.

P2 cut-over: per-user dispatch now lives in the reactive orchestrator, which wakes
``reactive.agents.curiosity.CuriosityThreadFollowUpAgent`` off a ``tick`` event and
runs it inside the Self-Heal Envelope. The old ``run_reflection_tick`` fan-out is
gone. This module keeps the pieces that are NOT per-user dispatch:
  * ``select_thread_to_follow_up`` — the pure selection of which open loop to ask about;
  * ``_build_thread_reason`` — the Buddy-facing "why I reached out" note;
  * ``on_thread_delivered`` — post-send bookkeeping the funnel drain calls on a real send.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ...lib.logger import logger
from ..analytics import posthog_client
from ..analytics.funnel_events import (
    EVENT_THREAD_FOLLOWUP_SENT,
    NOTIFICATION_ORIGIN_THREAD_ENGINE,
    PROP_NOTIFICATION_ORIGIN,
    PROP_THREAD_ID,
)
from ..notification_service import NotificationResult
from ..notifications.proposal import NotificationProposal
from . import thread_store
from .models import Thread, ThreadStatus

# ── Tuning constants (single source of truth for the thread engine) ─────────
# Most a single thread is ever followed up on before going dormant. Keeps Buddy
# from nagging about one loop the user clearly is not biting on.
MAX_FOLLOW_UPS_PER_THREAD = 2

# Do not ask about something the user mentioned moments ago — give the loop time
# to actually become an open question worth revisiting.
MIN_THREAD_AGE_BEFORE_FOLLOW_UP = timedelta(hours=1)

# Minimum gap between two follow-ups on the SAME thread.
FOLLOW_UP_COOLDOWN = timedelta(hours=20)

# After a REMINDER's due time (Thread.expected_resolution_at), give the user time to
# actually do the thing and let it settle before Buddy gets curious about it. Prevents
# the reminder's own committed push and the thread's curiosity follow-up landing
# minutes apart (root cause: scheduler.py scans due reminders every minute
# unconditionally while EVENT_TICK only wakes curiosity once/hour, so an unlucky due
# time near the top of the hour collided with an already-eligible thread). Also
# correctly defers follow-up on a reminder set far in the future, instead of the
# age-since-creation rule above firing well before the reminder is even due.
REMINDER_SETTLE_BUFFER = timedelta(hours=2)

# Hard ceiling on curiosity follow-ups per user per local day across all threads.
THREAD_DAILY_CAP = 1

NOTIFICATION_TYPE_THREAD_FOLLOW_UP = "thread_followup"

# Buddy-facing "why I reached out" note carried in the push payload and injected
# into the chat prompt on the FIRST turn after a tap, so if the user opens full
# chat Buddy stays oriented (it knows the thread it asked about) instead of
# disowning its own opener. Kept short; never shown to the user.
THREAD_REASON_MAX_CHARS = 600


def _build_thread_reason(thread: Thread) -> str:
    """Compose the Buddy-facing reason for a curiosity follow-up, deterministically
    from the thread (no extra LLM call, so it can never fail or drift). It anchors
    Buddy in WHAT it asked about and WHY (curiosity, not a task check-in)."""
    trigger = (thread.trigger_text or "").strip()
    known = (thread.known_summary or "").strip()
    parts: list[str] = []
    if trigger:
        parts.append(f'Earlier they mentioned: "{trigger}".')
    if known:
        parts.append(known if known.endswith(".") else known + ".")
    parts.append(
        "You sent a warm, curious follow-up about it because you genuinely wanted to "
        "know more, not to check whether they finished a task. Pick the thread back up "
        "naturally and stay curious."
    )
    return " ".join(parts)[:THREAD_REASON_MAX_CHARS]


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
        if thread.created_at is not None:
            if (now - thread.created_at) < MIN_THREAD_AGE_BEFORE_FOLLOW_UP:
                return False
        if thread.expected_resolution_at is not None:
            if now < thread.expected_resolution_at + REMINDER_SETTLE_BUFFER:
                return False
        if thread.last_follow_up_at is not None:
            if (now - thread.last_follow_up_at) < FOLLOW_UP_COOLDOWN:
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


async def on_thread_delivered(
    proposal: NotificationProposal, result: NotificationResult
) -> None:
    """Post-send bookkeeping for a thread follow-up the drain actually DELIVERED.

    Runs in the drain (via post_send.dispatch_post_send), not the reflector tick, so
    the per-thread + per-day counters, the funnel event, and the exhausted-thread
    retirement all key off a real send, never a held/dropped proposal. Never raises.
    """
    if not result.delivered:
        return
    user_id = proposal.user_id
    data = proposal.data or {}
    thread_id = data.get("thread_id", "")
    if not thread_id:
        return
    sent_at = datetime.now(UTC)
    local_date = data.get("local_date") or sent_at.date().isoformat()
    followups_before = int(data.get("followups_before", "0") or 0)

    await thread_store.mark_follow_up_sent(user_id, thread_id, sent_at)
    await thread_store.record_follow_up_in_budget(user_id, local_date, sent_at)

    # Top of the thread funnel. The join key + origin must match the client's
    # tap/session/reply events so PostHog can join sent -> tapped -> session -> reply.
    await posthog_client.capture_event(
        distinct_id=user_id,
        event=EVENT_THREAD_FOLLOWUP_SENT,
        properties={
            PROP_THREAD_ID: thread_id,
            PROP_NOTIFICATION_ORIGIN: NOTIFICATION_ORIGIN_THREAD_ENGINE,
            "source": data.get("thread_source", ""),
        },
    )

    # Retire a thread that has now exhausted its follow-up budget so it is never picked
    # again — the user clearly isn't biting. A later user reply moves it to ENGAGED.
    if followups_before + 1 >= MAX_FOLLOW_UPS_PER_THREAD:
        await thread_store.set_status(user_id, thread_id, ThreadStatus.DORMANT)

    logger.info("threads.thread_reflector: follow-up delivered", {
        "user_id": user_id, "thread_id": thread_id,
    })
