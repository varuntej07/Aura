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
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from pydantic import BaseModel

from ...lib.logger import logger
from ...prompts import (
    CONVERSATION_WORTHINESS_SYSTEM_PROMPT,
    REMINDER_WORTHINESS_SYSTEM_PROMPT,
    conversation_worthiness_user_prompt,
    reminder_worthiness_user_prompt,
)
from ..model_provider import get_model_provider
from . import thread_store
from .models import Thread, ThreadSource, ThreadStatus
from .sensitivity import classify_proactive_subject, read_graph_sensitivity_nodes

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


async def _judge_worth_a_thread(
    message: str,
    *,
    system: str = REMINDER_WORTHINESS_SYSTEM_PROMPT,
    build_user_prompt: Callable[[str], str] = reminder_worthiness_user_prompt,
) -> tuple[bool, str]:
    """Semantic worthiness judge (CLAUDE.md: teach a category, never a keyword
    list).

    The prompt pair is injectable so a conversation topic is judged against a
    stricter category than a reminder (a reminder is an explicit commitment; a
    conversation topic is not), while sharing this fail-closed wrapper.

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
                build_user_prompt(message),
                system=system,
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
    user_id: str, message: str, *, existing: list[Thread] | None = None
) -> Thread | None:
    """Find an existing thread on the SAME subject as ``message``, ANY status.

    ``existing`` lets a caller supply the thread list it already read (the
    conversation path judges several topics against one snapshot, and must also
    dedup each new topic against the threads it just created in the same pass).
    When omitted the list is read here, so the reminder path is unchanged.

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
    if existing is None:
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

    sensitivity = await classify_proactive_subject(message)
    if not sensitivity.allows_proactive:
        logger.info("threads.thread_writer: reminder curiosity suppressed", {
            "user_id": user_id,
            "reminder_id": reminder_id,
            "sensitivity_status": sensitivity.status,
            "sensitivity_source": sensitivity.source,
            "sensitivity_categories": sensitivity.categories,
        })
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
        sensitivity=sensitivity.to_dict(),
    )
    await thread_store.create_thread(user_id, thread)
    logger.info("threads.thread_writer: opened reminder thread", {
        "user_id": user_id,
        "thread_id": reminder_id,
    })


# ── Conversation-derived threads ─────────────────────────────────────────────
# The reminder wedge above only ever fires for users who set a reminder, which
# left the whole curiosity surface dark for everyone else — the single largest
# reason a first conversation produced no notifications. These open the same
# kind of loop from what the user actually talked about.

CONVERSATION_CURIOSITY_ANGLES = [
    "what is really going on with it",
    "why it matters to them",
    "where it has got to since",
]

# Most threads one session may open. A long session can cluster into many
# topics; opening a thread for each would hand the reflector a backlog it
# would drip out for weeks, long after the topics stopped being live.
MAX_THREADS_PER_SESSION = 3

# Topics judged per session, highest-turn-count first. Bounds the worthiness
# calls (one cheap LLM call each) so a 15-topic session cannot fan out. At
# hundreds of users with several sessions a day this ceiling is what keeps the
# judge spend proportional to sessions rather than to conversation length.
_MAX_TOPICS_JUDGED = 4

# Below this, a "topic" is a fragment (a greeting, a one-word reply) that the
# judge would only reject anyway. Filtered before the call, not after, so the
# cheap check runs first.
_MIN_TOPIC_SUMMARY_CHARS = 20


async def _assess_conversation_topic(
    user_id: str, topic: dict[str, Any]
) -> tuple[tuple[bool, str], Any | None]:
    summary = str(topic["summary"]).strip()
    worthiness = _judge_worth_a_thread(
        summary,
        system=CONVERSATION_WORTHINESS_SYSTEM_PROMPT,
        build_user_prompt=conversation_worthiness_user_prompt,
    )
    try:
        graph_nodes = await read_graph_sensitivity_nodes(
            user_id, [str(key) for key in topic.get("entity_keys") or []]
        )
    except Exception as exc:
        logger.error("threads.thread_writer: graph sensitivity read failed closed", {
            "user_id": user_id,
            "topic_id": topic.get("topic_id"),
            "error_type": type(exc).__name__,
        })
        return await worthiness, None
    sensitivity = classify_proactive_subject(
        summary,
        explicit_sensitive=any(
            turn.get("inferred_sensitive") is True for turn in topic.get("turns") or []
        ),
        graph_nodes=graph_nodes,
    )
    return await worthiness, await sensitivity


def _conversation_thread_id(session_id: str, topic_id: str) -> str:
    """Stable id for one topic of one session.

    ``create_thread`` is an idempotent *overwrite*, so a re-evaluated session
    must land on the same id rather than a fresh one — and the subject dedup
    below must still run, because overwriting an existing thread would reset
    ``follow_ups_sent`` and re-arm a follow-up budget the user already spent.
    """
    return f"conv_{session_id}_{topic_id}"


async def record_conversation_threads(
    user_id: str,
    *,
    session_id: str,
    topics: list[dict[str, Any]],
    surface: str = "",
) -> int:
    """Open curiosity threads for what the user talked about in one session.

    Returns the number of threads created. Safe to call fire-and-forget: it
    never raises, so session finalization is never affected by a judge timeout
    or a Firestore write error.

    Callers must already have applied the Aura consent gate — turns only exist
    to cluster when consent was granted, and a curiosity follow-up enriches
    UserAura, so the same GDPR gate that governs extraction governs this.
    """
    if not topics:
        return 0

    candidates = [
        topic for topic in topics
        if len(str(topic.get("summary") or "").strip()) >= _MIN_TOPIC_SUMMARY_CHARS
    ]
    candidates.sort(key=lambda topic: -int(topic.get("user_turn_count") or 0))
    candidates = candidates[:_MAX_TOPICS_JUDGED]
    if not candidates:
        return 0

    try:
        assessments = await asyncio.gather(*(
            _assess_conversation_topic(user_id, topic) for topic in candidates
        ))
    except Exception as exc:
        # _judge_worth_a_thread already fails closed per-topic; this only catches
        # a gather-level surprise. Loud, because a silent outage here would look
        # exactly like "no topics were interesting".
        logger.error("threads.thread_writer: conversation worthiness gather failed", {
            "user_id": user_id, "session_id": session_id, "error": str(exc),
        })
        return 0

    # One snapshot for the whole pass, extended in-place with what we create so
    # two similar topics in the same session cannot fork parallel threads.
    existing = await thread_store.list_threads_for_subject_dedup(user_id)
    now = datetime.now(UTC)
    source = ThreadSource.VOICE if surface == "voice" else ThreadSource.CHAT
    created = 0

    for topic, assessment in zip(candidates, assessments):
        if created >= MAX_THREADS_PER_SESSION:
            break
        (worth, reason), sensitivity = assessment
        summary = str(topic["summary"]).strip()
        if sensitivity is None or not sensitivity.allows_proactive:
            logger.info("threads.thread_writer: conversation curiosity suppressed", {
                "user_id": user_id,
                "session_id": session_id,
                "sensitivity_status": sensitivity.status if sensitivity else "unknown",
                "sensitivity_source": sensitivity.source if sensitivity else "graph_unavailable",
                "sensitivity_categories": sensitivity.categories if sensitivity else [],
            })
            continue
        if not worth:
            logger.info("threads.thread_writer: topic skipped, not worth a thread", {
                "user_id": user_id, "session_id": session_id, "reason": reason,
            })
            continue

        duplicate = await _find_existing_subject_thread(
            user_id, summary, existing=existing
        )
        if duplicate is not None:
            if duplicate.status == ThreadStatus.OPEN:
                await thread_store.touch_thread(user_id, duplicate.thread_id, now)
            logger.info("threads.thread_writer: topic already covered by a thread", {
                "user_id": user_id, "session_id": session_id,
                "thread_id": duplicate.thread_id, "status": str(duplicate.status),
            })
            continue

        sensitivity_doc = sensitivity.to_dict()
        sensitivity_doc["entity_keys"] = [
            str(key) for key in topic.get("entity_keys") or []
        ]
        thread = Thread(
            thread_id=_conversation_thread_id(session_id, str(topic["topic_id"])),
            trigger_text=summary,
            source=source,
            source_ref=session_id,
            known_summary=f"The user talked about: {summary}",
            unknown=list(CONVERSATION_CURIOSITY_ANGLES),
            created_at=now,
            last_touched_at=now,
            sensitivity=sensitivity_doc,
        )
        if not await thread_store.create_thread(user_id, thread):
            # The store logged the cause. Do not count it: a write that never
            # landed must not look like an opened thread in the logs.
            continue
        existing.append(thread)
        created += 1

    logger.info("threads.thread_writer: conversation threads opened", {
        "user_id": user_id,
        "session_id": session_id,
        "topics_judged": len(candidates),
        "created": created,
    })
    return created
