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
from .models import Thread, ThreadSource, ThreadStatus

# Generic curiosity angles for a reminder. The reflector's framer picks ONE and
# turns it into a specific, warm question — these are only the holes to aim at,
# never shown to the user verbatim.
REMINDER_CURIOSITY_ANGLES = [
    "what this is really about",
    "why it matters to them",
    "how it is going for them",
]

_WORTHINESS_TIMEOUT_S = 6.0  # guards a hung fire-and-forget task, not user latency

# Cosine threshold (gemini-embedding-001, 768-dim) above which a new reminder is
# treated as the SAME subject as an existing thread and reuses it instead of
# forking a parallel one. Mirrors the reminder-dedup threshold in tool_executor;
# conservative on purpose so two genuinely distinct loops are never merged.
THREAD_SUBJECT_SIMILARITY_THRESHOLD = 0.90


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0


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
    list).

    Fails CLOSED toward False (skip the thread) on any judge error, timeout, or
    malformed output. A curiosity thread is the lowest-value PROACTIVE surface,
    so for it silence beats a possibly-spammy push (the same "a low-value push is
    worse than silence" bar the notification tap-gate applies). This deliberately
    diverges from the fail-OPEN rule that governs COMMITTED sends — that rule
    exists so an infra blip never drops a notification the user explicitly asked
    for; a reminder's curiosity follow-up is not that. The failure is logged
    LOUDLY so an outage that silences threads is never invisible.
    """
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
        logger.error(
            "threads.thread_writer: worthiness judge unavailable, failing closed (skip thread)",
            {"error": str(exc)},
        )
        return False, "judge_unavailable"
    judgment = cast(_ReminderWorthinessJudgment, result)
    return bool(judgment.worth_asking_about), (judgment.reason or "").strip()[:60]


async def _find_existing_subject_thread(
    user_id: str, message: str
) -> Thread | None:
    """Find an existing thread on the SAME subject as ``message``, ANY status.

    Two layers, cheapest first (mirrors the reminder dedup in tool_executor):
      1. Exact casefolded ``trigger_text`` match — a pure re-set of the same loop.
         Deterministic, no embedding call, so it survives an embed outage.
      2. Semantic near-duplicate via one batched embedding call — the user
         re-worded the same subject ("the Annapurna project" vs "Annapurna labs").

    Time-independent on purpose: a reminder is one occasion, but a *curiosity
    loop* is one subject regardless of when it is re-reminded, so the fire-time
    window the reminder dedup uses does NOT apply here. Fails open to ``None`` (no
    match -> create the thread) on any read/embed error.
    """
    existing = await thread_store.list_threads_for_subject_dedup(user_id)
    if not existing:
        return None

    # Layer 1: exact text.
    normalized = message.strip().casefold()
    for thread in existing:
        if (thread.trigger_text or "").strip().casefold() == normalized:
            return thread

    # Layer 2: semantic near-duplicate.
    try:
        from ..signal_engine.embedder import embed_texts

        texts = [message] + [t.trigger_text for t in existing]
        vectors = await embed_texts(texts)
        if not vectors or not vectors[0]:
            return None
        new_vector = vectors[0]
        best: tuple[float, Thread] | None = None
        for thread, vector in zip(existing, vectors[1:]):
            score = _cosine(new_vector, vector)
            if score >= THREAD_SUBJECT_SIMILARITY_THRESHOLD and (best is None or score > best[0]):
                best = (score, thread)
        if best is not None:
            logger.info("threads.thread_writer: semantic subject match, reusing thread", {
                "user_id": user_id,
                "thread_id": best[1].thread_id,
                "similarity": round(best[0], 4),
            })
            return best[1]
    except Exception as exc:
        logger.warn("threads.thread_writer: subject semantic dedup failed; treating as new", {
            "user_id": user_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
    return None


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

    # Subject dedup: one curiosity loop per subject, not one per reminder_id. The
    # reminder id is a fresh uuid each time (tool_executor), so without this a
    # re-reminded subject forks a NEW thread that re-arms its own follow-up budget
    # and carries its own thread_id dedup_key — the funnel then can't see the two
    # are the same subject, and the user gets the same curiosity push again. See
    # the tracker fixtures redesign for the same identity-from-a-stable-key rule.
    existing = await _find_existing_subject_thread(user_id, message)
    if existing is not None:
        if existing.status == ThreadStatus.OPEN:
            # Still an open loop: a fresh mention makes it the most natural to ask
            # about next, so bump recency but keep the counters (no budget reset).
            await thread_store.touch_thread(user_id, existing.thread_id, now)
            logger.info("threads.thread_writer: reminder reuses open subject thread", {
                "user_id": user_id, "reminder_id": reminder_id,
                "thread_id": existing.thread_id,
            })
        else:
            # DORMANT / RESOLVED / ENGAGED: the subject was already explored or the
            # user is mid-conversation on it. Never resurrect it into a fresh
            # 2-follow-up budget — that is exactly the repeat-push bug.
            logger.info("threads.thread_writer: reminder subject already covered, skipping", {
                "user_id": user_id, "reminder_id": reminder_id,
                "thread_id": existing.thread_id, "status": str(existing.status),
            })
        return

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
