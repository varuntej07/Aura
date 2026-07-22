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
import json
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from ..config.settings import settings
from ..lib.logger import logger
from ..services.firebase import admin_firestore
from ..services.model_provider import get_model_provider
from . import voice_session_fields as vf
from .aura_reflection import consolidate_session
from .user_aura_extractor import extract_and_update_user_aura
from .voice_history_store import delete_voice_run_data
from .voice_transcript_reconciliation import reconcile_voice_transcript

_SESSION_SUMMARY_PROMPT = """\
Extract compact conversational memory from this voice transcript. Return only the
requested JSON object. Be specific and concrete. Do not infer that any reminder,
calendar event, message, or other side effect happened from transcript wording. Action
truth is added separately from runtime receipts and is not part of your output.

Transcript:
{transcript}

recap: one or two friendly sentences suitable for a history list.
open_loops: specific unfinished threads.
decisions: choices or commitments the user made, not tool actions.
emotional_context: one compact sentence, or empty.
facts: stable facts learned about the user's life.
follow_up: one natural question Buddy can ask next time, or empty.
"""


class VoiceSessionMemory(BaseModel):
    recap: str = ""
    open_loops: list[str] = Field(default_factory=list)
    decisions: list[str] = Field(default_factory=list)
    emotional_context: str = ""
    facts: list[str] = Field(default_factory=list)
    follow_up: str = ""

    def compact_context(self) -> str:
        return json.dumps(
            self.model_dump(exclude={"recap"}),
            ensure_ascii=False,
            separators=(",", ":"),
        )

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


async def _generate_session_summary(turns: list[dict]) -> VoiceSessionMemory:
    if not turns:
        return VoiceSessionMemory()
    transcript_lines = [
        f"{t['role']}: {t['text']}" for t in turns if t.get("text")
    ]
    if not transcript_lines:
        return VoiceSessionMemory()
    transcript = "\n".join(transcript_lines)
    prompt = _SESSION_SUMMARY_PROMPT.format(transcript=transcript)
    provider = get_model_provider()
    result = await provider.cheap(
        prompt,
        response_model=VoiceSessionMemory,
        temperature=0.2,
    )
    if isinstance(result, VoiceSessionMemory):
        return result
    # Compatibility for test doubles or a provider adapter returning plain text.
    return VoiceSessionMemory(recap=str(result).strip())


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
    memory: VoiceSessionMemory,
    raw_turns: list[dict],
    started_at: str,
    ended_at: str,
    duration_ms: int,
    tool_calls: list[str],
    screen_sight_frame_count: int,
    *,
    conversation_id: str,
    surface: str,
    action_receipts: list[dict],
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
        actions = [
            receipt
            for receipt in action_receipts
            if receipt.get(vf.ACTION_SUCCESS) is True
            and receipt.get(vf.ACTION_TOOL_NAME)
        ]
        schema_version = (
            vf.SCHEMA_VERSION_V2
            if conversation_id and surface != vf.SURFACE_UNKNOWN
            else 1
        )
        ref.set({
            vf.SCHEMA_VERSION: schema_version,
            vf.VOICE_RUN_ID: session_id,
            vf.CONVERSATION_ID: conversation_id,
            vf.SURFACE: surface,
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
            # Legacy readers keep using summary. For schema v2 it is the friendly
            # recap, while future voice context reads MEMORY_CONTEXT below.
            "summary": memory.recap,
            vf.RECAP: memory.recap,
            vf.OPEN_LOOPS: memory.open_loops,
            vf.DECISIONS: memory.decisions,
            vf.EMOTIONAL_CONTEXT: memory.emotional_context,
            vf.FACTS: memory.facts,
            vf.FOLLOW_UP: memory.follow_up,
            vf.MEMORY_CONTEXT: memory.compact_context(),
            vf.ACTIONS: actions,
            "archived": False,
            "raw_turns": raw_turns,
            # Count only — never the frame bytes themselves, which are never
            # persisted anywhere (see screen_frames.py's module docstring).
            "screen_sight_frame_count": screen_sight_frame_count,
        })
    await asyncio.to_thread(_write)


async def _write_latest_summary(
    user_id: str,
    memory: VoiceSessionMemory,
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
            "summary": memory.recap,
            vf.RECAP: memory.recap,
            vf.MEMORY_CONTEXT: memory.compact_context(),
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
                "summary": data.get(vf.MEMORY_CONTEXT) or data.get("summary", ""),
                "started_at": data.get("started_at", ""),
                vf.CONVERSATION_ID: data.get(vf.CONVERSATION_ID, ""),
                vf.VOICE_RUN_ID: data.get(vf.VOICE_RUN_ID, doc.id),
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
        existing_archive = archive_ref.get()
        existing_data = existing_archive.to_dict() or {} if existing_archive.exists else {}
        doc_ids = [s["doc_id"] for s in session_summaries]
        archived_run_ids = sorted({
            *existing_data.get("voice_run_ids", []),
            *(str(s.get(vf.VOICE_RUN_ID) or s["doc_id"]) for s in session_summaries),
        })
        archived_conversation_ids = sorted({
            *existing_data.get("conversation_ids", []),
            *(
                str(s.get(vf.CONVERSATION_ID))
                for s in session_summaries
                if s.get(vf.CONVERSATION_ID)
            ),
        })
        started_ats = [
            s.get("started_at", "") for s in session_summaries
        ]
        batch.set(archive_ref, {
            "archive_summary": archive_text,
            "last_archived_at": datetime.now(UTC).isoformat(),
            "sessions_archived_count": len(archived_run_ids),
            "voice_run_ids": archived_run_ids,
            "conversation_ids": archived_conversation_ids,
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
    try:
        results = await asyncio.gather(
            *(delete_voice_run_data(user_id, doc_id) for doc_id in doc_ids),
            return_exceptions=True,
        )
        failed = sum(
            1 for result in results
            if isinstance(result, BaseException) or not result.ok
        )
        logger.info("VoiceSession: cleanup complete", {
            "user_id": user_id,
            "deleted_count": len(doc_ids) - failed,
            "failed_count": failed,
        })
    except Exception as exc:
        logger.warn("VoiceSession: cleanup failed (cosmetic)", {
            "user_id": user_id, "error": str(exc),
        })


async def run_post_session_pipeline(
    user_id: str,
    session_id: str,
    conversation_id: str,
    surface: str,
    turns: list[dict],
    started_at: str,
    ended_at: str,
    duration_ms: int,
    tool_calls: list[str],
    action_receipts: list[dict] | None = None,
    screen_sight_frame_count: int = 0,
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

    memory: VoiceSessionMemory
    if isinstance(results_a[0], BaseException):
        logger.warn("VoiceSession: summary generation failed", {
            "user_id": user_id, "session_id": session_id,
            "error": str(results_a[0]),
        })
        memory = VoiceSessionMemory()
    else:
        generated = results_a[0]
        memory = (
            generated
            if isinstance(generated, VoiceSessionMemory)
            else VoiceSessionMemory(recap=str(generated).strip())
        )

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
            user_id, session_id, memory, turns,
            started_at, ended_at, duration_ms, tool_calls,
            screen_sight_frame_count,
            conversation_id=conversation_id,
            surface=surface,
            action_receipts=action_receipts or [],
        ),
        _write_latest_summary(
            user_id, memory, session_id, len(turns), duration_ms,
        ),
        extract_and_update_user_aura(
            uid=user_id,
            message=user_turns_text,
            session_id=session_id,
            turn_id=f"voice_summary_{session_id}",
            turn_index=max(0, len(turns) - 1),
            surface="voice",
        ) if user_turns_text else asyncio.sleep(0),
        # Reflection tier: the same per-session narrative pass text chat uses, so a
        # voice session also yields storylines/traits, not just flat interests.
        consolidate_session(
            user_id, session_id, turns, modality="voice",
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
    if isinstance(results_b[3], Exception):
        logger.warn("VoiceSession: aura reflection failed", {
            "user_id": user_id, "session_id": session_id,
            "error": str(results_b[3]),
        })

    # Canonical transcript repair is safe only for schema-v2 identity. The reconciler
    # verifies all present deterministic ids before inserting any missing child.
    if conversation_id:
        await reconcile_voice_transcript(
            user_id=user_id,
            conversation_id=conversation_id,
            voice_run_id=session_id,
            turns=turns,
        )

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
        "summary_len": len(memory.recap), "session_count": session_count,
    })
