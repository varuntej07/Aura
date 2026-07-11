"""Turn a user action into an open-loop thread.

v1 wedge: only user-set reminders. The reminder's own text *is* the thread —
the curiosity question is generated lazily, much later, by the reflector. A
worthiness judge (one cheap LLM call) runs here to skip mundane/administrative
reminders (a bill, a chore) that are not a genuine hole in what Buddy knows.
That call adds zero *perceived* latency: this function is always invoked via
``asyncio.create_task(...)`` from the reminder tool (fire-and-forget, never
awaited on the tool-call response path — see ``tool_executor.py``).
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast

from pydantic import BaseModel

from ...lib.logger import logger
from ..model_provider import get_model_provider
from . import thread_store
from .models import Thread, ThreadSource

# Generic curiosity angles for a reminder. The reflector's framer picks ONE and
# turns it into a specific, warm question — these are only the holes to aim at,
# never shown to the user verbatim.
REMINDER_CURIOSITY_ANGLES = [
    "what this is really about",
    "why it matters to them",
    "how it is going for them",
]

_WORTHINESS_TIMEOUT_S = 6.0  # guards a hung fire-and-forget task, not user latency


class _ReminderWorthinessJudgment(BaseModel):
    worth_asking_about: bool
    reason: str = ""


_WORTHINESS_SYSTEM = """\
You are a filter deciding whether a reminder is worth Buddy following up on later out
of genuine curiosity, the way a close friend would remember something you mentioned
and ask about it afterward.

Return ONLY JSON: {"worth_asking_about": true or false, "reason": "<=8 words"}

Approve (true) when the reminder points at something personal or emotionally rich —
a relationship, a decision, an event with stakes, a feeling — the kind of thing a
friend would naturally wonder "how did that go?" about.

Reject (false) when the reminder is a mundane routine or administrative task with
nothing to be curious about: a bill, a call/text to schedule/confirm/renew/book
something, a chore, a pickup, an errand. Nobody follows up on "did the plumber call
you back" like a friend would.

Examples:
"call mom" -> false (a routine check-in call, no real hook to be curious about)
"pay rent" -> false (administrative)
"call the bank about the lease deposit" -> false (routine, nothing to ask about)
"big presentation monday" -> true (something with stakes, worth checking in on)
"date night with sarah" -> true (personal, relationship)
"talk to my therapist about the job offer" -> true (emotionally rich, a real decision)
"renew car registration" -> false (administrative)

Be BALANCED: when in doubt, approve — a mediocre curiosity question is a minor cost,
a silently-dropped genuine loop is the real one. Reject only when it's clearly rote."""


async def _judge_worth_a_thread(message: str) -> tuple[bool, str]:
    """Semantic worthiness judge (CLAUDE.md: teach a category, never a keyword
    list). Fails OPEN toward True (create the thread — today's existing
    behavior) on any judge error, timeout, or malformed output, matching this
    codebase's universal fail-open philosophy (see notifications/tap_gate.py)."""
    try:
        result = await asyncio.wait_for(
            get_model_provider().cheap(
                f'Reminder text: "{message}"\n\nIs this worth following up on?',
                system=_WORTHINESS_SYSTEM,
                response_model=_ReminderWorthinessJudgment,
                temperature=0.0,
            ),
            timeout=_WORTHINESS_TIMEOUT_S,
        )
    except Exception as exc:
        logger.warn(
            "threads.thread_writer: worthiness judge unavailable, failing open (create thread)",
            {"error": str(exc)},
        )
        return True, "judge_unavailable"
    judgment = cast(_ReminderWorthinessJudgment, result)
    return bool(judgment.worth_asking_about), (judgment.reason or "").strip()[:60]


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


async def record_reminder_thread(
    user_id: str,
    *,
    reminder_id: str,
    message: str,
    trigger_at_iso: str,
) -> None:
    """Open a curiosity thread for a freshly created reminder, unless it's a
    mundane/administrative task not worth a curiosity follow-up.

    Safe to call fire-and-forget: it never raises (the store swallows write
    errors, and the worthiness judge fails open), so the chat/voice tool path
    is never affected.
    """
    message = (message or "").strip()
    if not message:
        return

    worth, reason = await _judge_worth_a_thread(message)
    if not worth:
        logger.info("threads.thread_writer: reminder skipped, not worth a curiosity thread", {
            "user_id": user_id, "reminder_id": reminder_id, "reason": reason,
        })
        return

    now = datetime.now(UTC)
    thread = Thread(
        thread_id=reminder_id,                       # 1:1 with the reminder; idempotent
        trigger_text=message,
        source=ThreadSource.REMINDER,
        source_ref=reminder_id,
        known_summary=f"The user set a reminder about: {message}",
        unknown=list(REMINDER_CURIOSITY_ANGLES),
        created_at=now,
        last_touched_at=now,
        expected_resolution_at=_parse_iso(trigger_at_iso),
    )
    await thread_store.create_thread(user_id, thread)
    logger.info("threads.thread_writer: opened reminder thread", {
        "user_id": user_id,
        "thread_id": reminder_id,
    })
