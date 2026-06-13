"""
Post-session pipeline for voice session memory.

Runs as fire-and-forget after each voice call ends. Generates a structured
summary via Gemini Flash, persists it to Firestore, and triggers archive
synthesis when the active session count exceeds 30.

All steps use asyncio.gather(..., return_exceptions=True) so no single
failure kills another step. Archive writes use a Firestore WriteBatch for
atomicity: the archive doc and all archived flags are committed together.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from ..config.settings import settings
from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.model_provider import get_model_provider
from .user_aura_extractor import extract_and_update_user_aura

_SESSION_SUMMARY_PROMPT = """\
You are extracting structured memory from a voice conversation between a user
and their AI friend Buddy. Extract SPECIFIC CONCRETE facts only. Never be vague.

Good: "user wants to run 5km by July 15"
Bad:  "user mentioned fitness goals"

Transcript:
{transcript}

Extract in these exact categories. Write "none" if a category has nothing.

OPEN LOOPS
Things the user mentioned wanting to do but did not complete this call.
One bullet per item. Include specifics: dates, names, numbers.

DECISIONS MADE
Things the user decided or committed to during this call.

EMOTIONAL STATE
How the user seemed. Be specific: "frustrated about sleep onset, said it takes
2 hours to fall asleep" not "mentioned sleep issues".

REMINDERS AND CALENDAR
Tool calls made: reminders set, calendar events created. Include exact times.

KEY FACTS LEARNED
New facts about the user's life. Job, relationships, health, location, habits.
Only include things not likely mentioned before.

FOLLOW-UP HOOK
One specific question Buddy can ask next session to show continuity.
Example: "Ask if they started the 5km training they mentioned for July"
"""

_ARCHIVE_SYNTHESIS_PROMPT = """\
You are building a long-term memory profile from {n} past voice conversation
summaries between a user and their AI friend Buddy. Each summary is from one
session. Be specific and concrete. This context is injected into future sessions.

PAST SESSION SUMMARIES (oldest to newest):
{summaries}

Categories:

RECURRING THEMES
Topics appearing in 3 or more sessions. How they evolved over time if visible.

LONG-TERM GOALS
Goals mentioned multiple times. Note progress across sessions if visible.

LIFE FACTS
Stable facts about the user: job, relationships, health conditions, location,
routines. Only include things mentioned consistently across multiple sessions.

BEHAVIORAL PATTERNS
How this user tends to behave. What motivates them, what they worry about,
how they handle stress, patterns consistent with ADHD if evident.

RESOLVED THREADS
Things that were open loops in early sessions and appear resolved later.

PERSISTENT OPEN LOOPS
Things the user keeps mentioning across sessions but has not resolved.

BUDDY-USER RELATIONSHIP
Key moments in the relationship. Running references or inside jokes.
Things that landed well. Things that caused friction or fell flat.
"""

_ACTIVE_SESSION_THRESHOLD = 30
_ARCHIVE_BATCH_SIZE = 25


def _format_session_duration(duration_ms: int) -> str:
    total_seconds = duration_ms // 1000
    minutes, seconds = divmod(total_seconds, 60)
    if minutes and seconds:
        return f"{minutes}m {seconds}s"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


async def _generate_session_summary(turns: list[dict]) -> str:
    if not turns:
        return ""
    transcript_lines = [
        f"{t['role']}: {t['text']}" for t in turns if t.get("text")
    ]
    if not transcript_lines:
        return ""
    transcript = "\n".join(transcript_lines)
    prompt = _SESSION_SUMMARY_PROMPT.format(transcript=transcript)
    provider = get_model_provider()
    return await provider.cheap(prompt, temperature=0.3)


async def _count_active_sessions(user_id: str) -> int:
    def _count() -> int:
        coll = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_sessions")
        )
        result = coll.where("archived", "==", False).count().get()
        return result[0][0].value
    return await asyncio.to_thread(_count)


async def _write_session_doc(
    user_id: str,
    session_id: str,
    summary: str,
    raw_turns: list[dict],
    started_at: str,
    ended_at: str,
    duration_ms: int,
    tool_calls: list[str],
) -> None:
    def _write() -> None:
        ref = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_sessions").document(session_id)
        )
        num_of_user_turns = sum(1 for t in raw_turns if t.get("role") == "user")
        num_of_assistant_turns = sum(
            1 for t in raw_turns if t.get("role") == "assistant"
        )
        ref.set({
            "started_at": started_at,
            "ended_at": ended_at,
            "duration_ms": duration_ms,
            "total_duration": _format_session_duration(duration_ms),
            "num_of_turns": len(raw_turns),
            "num_of_user_turns": num_of_user_turns,
            "num_of_assistant_turns": num_of_assistant_turns,
            "tool_calls_made": tool_calls,
            "num_of_tool_calls": len(tool_calls),
            "model_used": settings.ANTHROPIC_VOICE_MODEL,
            "summary": summary,
            "archived": False,
            "raw_turns": raw_turns,
        })
    await asyncio.to_thread(_write)


async def _write_latest_summary(
    user_id: str,
    summary: str,
    session_id: str,
    turn_count: int,
    duration_ms: int,
) -> None:
    def _write() -> None:
        ref = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_session_state").document("latest")
        )
        ref.set({
            "summary": summary,
            "last_session_at": datetime.now(UTC).isoformat(),
            "last_session_id": session_id,
            "turn_count": turn_count,
            "duration_ms": duration_ms,
        })
    await asyncio.to_thread(_write)


async def _fetch_oldest_active_summaries(
    user_id: str, limit: int = _ARCHIVE_BATCH_SIZE
) -> list[dict]:
    def _read() -> list[dict]:
        coll = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_sessions")
        )
        query = (
            coll.where("archived", "==", False)
            .order_by("started_at")
            .limit(limit)
        )
        results: list[dict] = []
        for doc in query.stream():
            data = doc.to_dict() or {}
            results.append({
                "doc_id": doc.id,
                "summary": data.get("summary", ""),
                "started_at": data.get("started_at", ""),
            })
        return results
    return await asyncio.to_thread(_read)


async def _fetch_existing_archive_text(user_id: str) -> str:
    """Read the existing archive summary so it can be included in the next synthesis.

    Without this, each archive cycle overwrites the previous one and history
    older than _ARCHIVE_BATCH_SIZE sessions is permanently lost.
    """
    def _read() -> str:
        doc = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_session_state").document("archive")
            .get()
        )
        data = doc.to_dict() or {}
        return str(data.get("archive_summary", ""))
    return await asyncio.to_thread(_read)


async def _synthesize_archive(
    session_summaries: list[dict],
    existing_archive: str = "",
) -> str:
    new_summaries_text = "\n\n---\n\n".join(
        f"Session {i + 1}:\n{s['summary']}"
        for i, s in enumerate(session_summaries)
        if s.get("summary")
    )
    if not new_summaries_text:
        return ""
    prior_section = (
        f"PRIOR ARCHIVE (from earlier sessions):\n{existing_archive}\n\n---\n\n"
        if existing_archive.strip()
        else ""
    )
    prompt = _ARCHIVE_SYNTHESIS_PROMPT.format(
        n=len(session_summaries),
        summaries=prior_section + new_summaries_text,
    )
    provider = get_model_provider()
    return await provider.cheap(prompt, temperature=0.4)


async def _archive_sessions(
    user_id: str,
    session_summaries: list[dict],
    archive_text: str,
) -> None:
    def _batch_write() -> list[str]:
        db = admin_firestore()
        batch = db.batch()

        archive_ref = (
            db.collection("users").document(user_id)
            .collection("voice_session_state").document("archive")
        )
        doc_ids = [s["doc_id"] for s in session_summaries]
        started_ats = [
            s.get("started_at", "") for s in session_summaries
        ]
        batch.set(archive_ref, {
            "archive_summary": archive_text,
            "last_archived_at": datetime.now(UTC).isoformat(),
            "sessions_archived_count": len(doc_ids),
            "oldest_archived_session_at": started_ats[0] if started_ats else "",
            "newest_archived_session_at": started_ats[-1] if started_ats else "",
        })

        for doc_id in doc_ids:
            session_ref = (
                db.collection("users").document(user_id)
                .collection("voice_sessions").document(doc_id)
            )
            batch.update(session_ref, {"archived": True})

        batch.commit()
        return doc_ids

    doc_ids = await asyncio.to_thread(_batch_write)
    logger.info("VoiceSession: archive batch committed", {
        "user_id": user_id, "archived_count": len(doc_ids),
    })
    asyncio.create_task(
        _cleanup_archived_docs(user_id, doc_ids),
        name=f"voice-archive-cleanup-{user_id}",
    )


async def _cleanup_archived_docs(user_id: str, doc_ids: list[str]) -> None:
    def _delete() -> None:
        db = admin_firestore()
        for doc_id in doc_ids:
            try:
                db.collection("users").document(user_id).collection(
                    "voice_sessions"
                ).document(doc_id).delete()
            except Exception as exc:
                logger.warn("VoiceSession: cleanup delete failed", {
                    "user_id": user_id, "doc_id": doc_id, "error": str(exc),
                })
    try:
        await asyncio.to_thread(_delete)
        logger.info("VoiceSession: cleanup complete", {
            "user_id": user_id, "deleted_count": len(doc_ids),
        })
    except Exception as exc:
        logger.warn("VoiceSession: cleanup failed (cosmetic)", {
            "user_id": user_id, "error": str(exc),
        })


async def run_post_session_pipeline(
    user_id: str,
    session_id: str,
    turns: list[dict],
    started_at: str,
    ended_at: str,
    duration_ms: int,
    tool_calls: list[str],
) -> None:
    logger.info("VoiceSession: post-session pipeline started", {
        "user_id": user_id, "session_id": session_id,
        "turn_count": len(turns), "duration_ms": duration_ms,
    })

    # Step A: summary + count in parallel
    results_a = await asyncio.gather(
        _generate_session_summary(turns),
        _count_active_sessions(user_id),
        return_exceptions=True,
    )

    summary: str
    if isinstance(results_a[0], BaseException):
        logger.warn("VoiceSession: summary generation failed", {
            "user_id": user_id, "session_id": session_id,
            "error": str(results_a[0]),
        })
        summary = ""
    else:
        summary = str(results_a[0])

    session_count: int
    if isinstance(results_a[1], BaseException):
        logger.warn("VoiceSession: session count failed", {
            "user_id": user_id, "session_id": session_id,
            "error": str(results_a[1]),
        })
        session_count = 0
    else:
        session_count = int(results_a[1])

    # Step B: persist session doc + latest summary + aura profile in parallel
    user_turns_text = "\n".join(
        t["text"] for t in turns if t.get("role") == "user" and t.get("text")
    )
    results_b = await asyncio.gather(
        _write_session_doc(
            user_id, session_id, summary, turns,
            started_at, ended_at, duration_ms, tool_calls,
        ),
        _write_latest_summary(
            user_id, summary, session_id, len(turns), duration_ms,
        ),
        extract_and_update_user_aura(
            uid=user_id,
            message=user_turns_text,
            session_id=session_id,
        ) if user_turns_text else asyncio.sleep(0),
        return_exceptions=True,
    )

    if isinstance(results_b[0], Exception):
        logger.warn("VoiceSession: session doc write failed", {
            "user_id": user_id, "session_id": session_id,
            "error": str(results_b[0]),
        })
    if isinstance(results_b[1], Exception):
        logger.warn("VoiceSession: latest summary write failed", {
            "user_id": user_id, "session_id": session_id,
            "error": str(results_b[1]),
        })
    if isinstance(results_b[2], Exception):
        logger.warn("VoiceSession: aura extraction failed", {
            "user_id": user_id, "session_id": session_id,
            "error": str(results_b[2]),
        })

    # Step C: archive check
    if session_count > _ACTIVE_SESSION_THRESHOLD:
        try:
            oldest, existing_archive = await asyncio.gather(
                _fetch_oldest_active_summaries(user_id),
                _fetch_existing_archive_text(user_id),
                return_exceptions=True,
            )
            if isinstance(oldest, BaseException):
                raise oldest
            if isinstance(existing_archive, BaseException):
                existing_archive = ""
            if oldest:
                archive_text = await _synthesize_archive(oldest, existing_archive)
                if archive_text:
                    await _archive_sessions(user_id, oldest, archive_text)
        except Exception as exc:
            logger.warn("VoiceSession: archive cycle failed", {
                "user_id": user_id, "session_id": session_id,
                "error": str(exc),
            })

    logger.info("VoiceSession: post-session pipeline complete", {
        "user_id": user_id, "session_id": session_id,
        "summary_len": len(summary), "session_count": session_count,
    })
