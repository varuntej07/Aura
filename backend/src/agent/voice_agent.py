"""
LiveKit voice agent using cascading architecture: STT -> LLM -> TTS

Pipeline plugins:
  Deepgram Nova STT (with nova-3 -> nova-2 fallback)
  Anthropic Claude LLM (with Gemini Flash fallback)
  Cartesia TTS (sonic-3 -> sonic-2 fallback)
  Silero VAD + LiveKit MultilingualModel turn detector

Tools live in the FastAPI backend at POST /mcp and are pulled in via
livekit.agents.mcp.MCPServerHTTP. The worker authenticates to /mcp with a
Firebase ID token it derives per-session from the user's uid (Admin SDK
custom token -> identitytoolkit exchange).

The worker connects to LiveKit Cloud and waits for participant joins. When
a Flutter client joins room "voice-{uid}", this agent starts a session.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from livekit.agents import (
    AgentSession,
    JobContext,
    JobProcess,
    TurnHandlingOptions,
    WorkerOptions,
    cli,
    mcp,
)
from livekit.agents import llm as lk_llm
from livekit.agents import stt as lk_stt
from livekit.agents import tts as lk_tts
from livekit.agents.voice import room_io
from livekit.plugins import anthropic, cartesia, deepgram, google, silero
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from ..config.settings import settings
from ..lib.logger import logger
from ..services.firebase import admin_auth, admin_firestore
from ..services.voice_session_summarizer import run_post_session_pipeline
from .buddy_agent import BuddyAgent

# Firebase auto-issued UIDs are 28 alphanumeric chars.
# We refuse anything else so a malformed room name can't drive a session.
_FIREBASE_UID_RE = re.compile(r"^[A-Za-z0-9]{28}$")

# Spoken in parallel with each MCP tool round-trip so the user hears on-line feedback that the agent is working on it.
_TOOL_THINKING_PHRASES: dict[str, str] = {
    "get_upcoming_events": "Alright pulling up your calendar right now!",
    "create_calendar_event": "Cool, adding that to your calendar now!",
    "set_reminder": "Gotcha, setting that reminder for you!",
    "cancel_reminder": "Heard that, taking care of that reminder now...",
    "list_reminders": "pulling up your reminders for you!",
    "store_memory": "Ah huh, got it, I'll keep that in mind!",
    "query_memory": "thinking through what I remember about you...",
    "analyze_nutrition": "having a closer look at that...",
    "get_user_context": "pulling up your details for this!",
    "web_search": "surfing the web for you right now!",
}

# Hard cap on the parallel profile + memory fetch before session.start.
# A LiveKit session can't speak its greeting until on_enter resolves, and
# the agent feels conversational only if the first audio lands inside ~1s.
# 1.5s is the budget that still leaves margin for STT/LLM/TTS warm-up.
_PRE_SESSION_FETCH_TIMEOUT_S = 1.5


def _log_voice_failure(
    *,
    code: str,
    user_id: str,
    room_name: str,
    session_id: str | None,
    exc: Exception,
) -> None:
    logger.error("VoiceSession: failure", {
        "code": code,
        "user_id": user_id,
        "room": room_name,
        "session_id": session_id,
        "error_type": type(exc).__name__,
        "error": str(exc),
    })


@asynccontextmanager
async def _voice_session_context(user_id: str, room_name: str) -> AsyncIterator[str]:
    session_id = str(uuid4())
    start = time.monotonic()
    logger.info("VoiceSession: started", {
        "session_id": session_id, "user_id": user_id, "room": room_name,
    })
    error: Exception | None = None
    try:
        yield session_id
    except Exception as exc:
        error = exc
        logger.exception("VoiceSession: unhandled error", {
            "session_id": session_id, "user_id": user_id,
            "error_type": type(exc).__name__, "error": str(exc),
        })
        raise
    finally:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.info("VoiceSession: closed", {
            "session_id": session_id, "user_id": user_id,
            "duration_ms": elapsed_ms,
            "error": str(error) if error else None,
        })


def _local_time_in_zone(timezone_name: str) -> str:
    """Format current wall-clock time in the user's timezone for the prompt."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.now(ZoneInfo(timezone_name)).strftime("%-I:%M %p")
    except Exception:
        from datetime import datetime
        return datetime.now(UTC).strftime("%H:%M UTC")


def _local_date_in_zone(timezone_name: str) -> str:
    """Format today's date in the user's timezone for the prompt (e.g. 'Thursday, 28 May 2026')."""
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime
        return datetime.now(ZoneInfo(timezone_name)).strftime("%A, %-d %B %Y")
    except Exception:
        from datetime import datetime
        return datetime.now(UTC).strftime("%A, %-d %B %Y UTC")


async def _fetch_user_profile(user_id: str) -> dict[str, str]:
    """Return {name, timezone} from users/{uid}. Defaults fill missing fields."""
    def _read() -> dict[str, str]:
        doc = admin_firestore().collection("users").document(user_id).get()
        data = doc.to_dict() or {}
        return {
            "name": (data.get("display_name") or data.get("name") or "").strip() or "there",
            "timezone": (data.get("timezone") or "UTC").strip() or "UTC",
        }
    return await asyncio.to_thread(_read)


async def _fetch_memory_summary(user_id: str) -> str:
    """Top 5 recent rows from users/{uid}/memories, formatted as bullet lines."""
    def _read() -> str:
        coll = admin_firestore().collection("users").document(user_id).collection("memories")
        try:
            docs = list(coll.order_by("updated_at", direction="DESCENDING").limit(5).stream())
        except Exception:
            docs = list(coll.limit(5).stream())
        if not docs:
            return ""
        lines: list[str] = []
        for d in docs:
            row = d.to_dict() or {}
            key = str(row.get("key", "")).strip()
            value = str(row.get("value", "")).strip()
            if key and value:
                lines.append(f"- {key}: {value}")
        return "\n".join(lines)
    return await asyncio.to_thread(_read)


async def _fetch_last_session_summary(user_id: str) -> dict[str, str]:
    """Read users/{uid}/voice_session_state/latest. Returns {summary, last_session_at} or empty."""
    def _read() -> dict[str, str]:
        doc = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_session_state").document("latest")
            .get()
        )
        data = doc.to_dict() or {}
        return {
            "summary": str(data.get("summary", "")),
            "last_session_at": str(data.get("last_session_at", "")),
        }
    return await asyncio.to_thread(_read)


async def _fetch_archive_context(user_id: str) -> dict[str, str]:
    """Read users/{uid}/voice_session_state/archive. Returns {archive_summary} or empty."""
    def _read() -> dict[str, str]:
        doc = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_session_state").document("archive")
            .get()
        )
        data = doc.to_dict() or {}
        return {"archive_summary": str(data.get("archive_summary", ""))}
    return await asyncio.to_thread(_read)


async def _mint_firebase_id_token(user_id: str) -> str:
    """Exchange an Admin-SDK custom token for a real Firebase ID token.

    The /mcp endpoint verifies tokens with admin_auth().verify_id_token, which
    only accepts ID tokens, not custom tokens. To stay on a single auth path
    (same as /chat) the worker mints a custom token and swaps it via the
    identitytoolkit REST endpoint. Requires FIREBASE_WEB_API_KEY.
    """
    if not settings.FIREBASE_WEB_API_KEY:
        raise RuntimeError(
            "FIREBASE_WEB_API_KEY is not configured — voice worker cannot reach /mcp"
        )

    custom_token = admin_auth().create_custom_token(user_id)
    if isinstance(custom_token, bytes):
        custom_token = custom_token.decode("utf-8")

    url = (
        "https://identitytoolkit.googleapis.com/v1/accounts:signInWithCustomToken"
        f"?key={settings.FIREBASE_WEB_API_KEY.strip()}"
    )
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            url,
            json={"token": custom_token, "returnSecureToken": True},
        )
        resp.raise_for_status()
        body = resp.json()
        id_token = body.get("idToken")
        if not isinstance(id_token, str) or not id_token:
            raise RuntimeError("identitytoolkit response missing idToken")
        return id_token


def prewarm(process: JobProcess) -> None:
    logger.info("VoiceWorker: prewarming VAD model")
    process.userdata["vad"] = silero.VAD.load()


async def entrypoint(ctx: JobContext) -> None:
    logger.info("VoiceAgent: job dispatched", {"room": ctx.room.name})
    candidate_user_id = ctx.room.name.removeprefix("voice-")
    try:
        await asyncio.wait_for(ctx.connect(), timeout=settings.VOICE_CONNECT_TIMEOUT_S)
    except TimeoutError:
        logger.error("VoiceAgent: room connect timed out", {"room": ctx.room.name})
        _log_voice_failure(
            code="room_connect_timeout",
            user_id=candidate_user_id,
            room_name=ctx.room.name,
            session_id=None,
            exc=TimeoutError("LiveKit ctx.connect() timeout"),
        )
        return
    except Exception as exc:
        logger.exception("VoiceAgent: room connect failed", {"room": ctx.room.name, "error": str(exc)})
        _log_voice_failure(
            code="room_connect_failed",
            user_id=candidate_user_id,
            room_name=ctx.room.name,
            session_id=None,
            exc=exc,
        )
        return

    user_id = candidate_user_id
    if not _FIREBASE_UID_RE.match(user_id):
        logger.error("VoiceAgent: invalid uid in room name", {
            "room": ctx.room.name, "extracted_uid": user_id,
        })
        return

    async with _voice_session_context(user_id, ctx.room.name) as session_id:
        # Fetch user profile, memory, last session, and archive in parallel.
        # The hard 1.5s ceiling enforces the under-1s greeting feel.
        # Each fetch defaults independently on failure.
        try:
            fetch_results = await asyncio.wait_for(
                asyncio.gather(
                    _fetch_user_profile(user_id),
                    _fetch_memory_summary(user_id),
                    _fetch_last_session_summary(user_id),
                    _fetch_archive_context(user_id),
                    return_exceptions=True,
                ),
                timeout=_PRE_SESSION_FETCH_TIMEOUT_S,
            )
        except TimeoutError:
            logger.warn("VoiceSession: pre-session fetch timed out, using defaults", {
                "session_id": session_id, "user_id": user_id,
            })
            fetch_results = [
                {"name": "there", "timezone": "UTC"},
                "",
                {"summary": "", "last_session_at": ""},
                {"archive_summary": ""},
            ]

        profile = fetch_results[0] if not isinstance(fetch_results[0], BaseException) else {"name": "there", "timezone": "UTC"}
        memory_summary = fetch_results[1] if not isinstance(fetch_results[1], BaseException) else ""
        last_session = fetch_results[2] if not isinstance(fetch_results[2], BaseException) else {"summary": "", "last_session_at": ""}
        archive_data = fetch_results[3] if not isinstance(fetch_results[3], BaseException) else {"archive_summary": ""}

        for i, r in enumerate(fetch_results):
            if isinstance(r, BaseException):
                logger.warn("VoiceSession: pre-session fetch failed", {
                    "session_id": session_id, "user_id": user_id,
                    "index": i, "error": str(r),
                })

        last_session_summary = last_session.get("summary", "") if isinstance(last_session, dict) else ""
        last_session_at = last_session.get("last_session_at", "") if isinstance(last_session, dict) else ""
        archive_context = archive_data.get("archive_summary", "") if isinstance(archive_data, dict) else ""

        context_vars = {
            "name": profile["name"],
            "timezone": profile["timezone"],
            "local_time": _local_time_in_zone(profile["timezone"]),
            "local_date": _local_date_in_zone(profile["timezone"]),
            "memory_summary": memory_summary or "(nothing yet — first conversation)",
            "last_session_context": last_session_summary,
            "last_session_at": last_session_at,
            "archive_context": archive_context,
        }

        # Seed the chat history with a system-side note that mirrors what the prompt already says. 
        # Keeps the model anchored on the memory across turns without round-tripping to the LLM up front.
        chat_ctx = lk_llm.ChatContext()
        if memory_summary:
            chat_ctx.add_message(
                role="system",
                content=(
                    "Memory of prior chats with this user:\n"
                    f"{memory_summary}"
                ),
            )

        # Mint a Firebase ID token so the MCP server can verify the worker.
        # Failure is fatal for tool use so the session can still hold a
        # conversation but tools won't work, so we log loudly and bail.
        try:
            firebase_id_token = await _mint_firebase_id_token(user_id)
        except Exception as exc:
            _log_voice_failure(
                code="mcp_token_mint_failed",
                user_id=user_id,
                room_name=ctx.room.name,
                session_id=session_id,
                exc=exc,
            )
            return

        stt_pipeline = lk_stt.FallbackAdapter(
            [
                deepgram.STT(model="nova-3", api_key=settings.DEEPGRAM_API_KEY.strip()),
                deepgram.STT(model="nova-2", api_key=settings.DEEPGRAM_API_KEY.strip()),
            ],
            attempt_timeout=10.0,
            max_retry_per_stt=1,
            retry_interval=0.5,
        )

        llm_pipeline = lk_llm.FallbackAdapter(
            [
                anthropic.LLM(model=settings.ANTHROPIC_CHAT_MODEL, api_key=settings.ANTHROPIC_API_KEY.strip()),
                google.LLM(model=settings.TIER_CHEAP, api_key=settings.GEMINI_API_KEY.strip()),
            ],
            attempt_timeout=12.0,
        )

        tts_pipeline = lk_tts.FallbackAdapter(
            [
                cartesia.TTS(api_key=settings.CARTESIA_API_KEY.strip(), model="sonic-3"),
                cartesia.TTS(api_key=settings.CARTESIA_API_KEY.strip(), model="sonic-2"),
            ],
            max_retry_per_tts=1,
        )

        mcp_url = f"{settings.BACKEND_INTERNAL_URL.rstrip('/')}/mcp/"
        mcp_server = mcp.MCPServerHTTP(
            url=mcp_url,
            transport_type="streamable_http",
            headers={"Authorization": f"Bearer {firebase_id_token}"},
        )

        try:
            turn_detector = MultilingualModel()
        except Exception as exc:
            _log_voice_failure(
                code="turn_detector_init_failed",
                user_id=user_id,
                room_name=ctx.room.name,
                session_id=session_id,
                exc=exc,
            )
            return

        session = AgentSession(
            stt=stt_pipeline,
            llm=llm_pipeline,
            tts=tts_pipeline,
            vad=ctx.proc.userdata["vad"],
            turn_detection=turn_detector,
            preemptive_generation=True,
            mcp_servers=[mcp_server],
            turn_handling=TurnHandlingOptions(
                interruption={
                    "mode": "adaptive",
                    "false_interruption_timeout": 2.0,
                    "resume_false_interruption": True,
                },
            ),
        )

        buddy = BuddyAgent(
            user_id=user_id,
            context_vars=context_vars,
            chat_ctx=chat_ctx,
        )
        session_done = asyncio.Event()
        session_turns: list[dict] = []
        session_tool_calls: list[str] = []
        session_start_iso = datetime.now(UTC).isoformat()
        session_start_mono = time.monotonic()

        @session.on("agent_state_changed")
        def _on_state(ev) -> None:  # type: ignore[misc]
            state = str(getattr(ev, "new_state", ""))
            logger.info("VoiceSession: agent_state_changed", {
                "session_id": session_id, "user_id": user_id,
                "state": state,
            })

        @session.on("user_input_transcribed")
        def _on_user_transcript(ev) -> None:  # type: ignore[misc]
            logger.info("VoiceSession: STT transcript", {
                "session_id": session_id, "user_id": user_id,
                "text": ev.transcript, "is_final": ev.is_final,
            })
            if ev.is_final and ev.transcript:
                session_turns.append({
                    "role": "user",
                    "text": ev.transcript,
                    "timestamp": datetime.now(UTC).isoformat(),
                })

        @session.on("conversation_item_added")
        def _on_conversation_item(ev) -> None:  # type: ignore[misc]
            item = getattr(ev, "item", None)
            if item is None:
                return

            if getattr(item, "role", None) == "assistant":
                content = getattr(item, "text_content", None) or str(item)
                logger.info("VoiceSession: agent response", {
                    "session_id": session_id, "user_id": user_id,
                    "text_preview": str(content)[:120],
                })
                session_turns.append({
                    "role": "assistant",
                    "text": str(content)[:500],
                    "timestamp": datetime.now(UTC).isoformat(),
                })

            # Fire a per-tool phrase in parallel with the MCP round-trip.
            # Gated on agent_state == "thinking" at fire-time so a phrase never
            # lands on top of the model's actual reply if the tool returns fast.
            tool_calls = getattr(item, "tool_calls", None) or []
            if tool_calls:
                tool_name = getattr(tool_calls[0], "name", "") or ""
                if tool_name:
                    session_tool_calls.append(tool_name)
                phrase = _TOOL_THINKING_PHRASES.get(tool_name)
                if phrase:
                    async def _speak_tool_phrase(p: str = phrase, name: str = tool_name) -> None:
                        if str(getattr(session, "agent_state", "")) != "thinking":
                            logger.info("VoiceSession: tool phrase skipped (not thinking)", {
                                "session_id": session_id, "user_id": user_id, "tool": name,
                            })
                            return
                        try:
                            await session.say(p, allow_interruptions=True, add_to_chat_ctx=False)
                        except Exception as exc:
                            logger.warn("VoiceSession: tool phrase failed", {
                                "session_id": session_id, "user_id": user_id, "error": str(exc),
                            })

                    asyncio.create_task(
                        _speak_tool_phrase(),
                        name=f"tool-phrase-{tool_name}-{session_id[:8]}",
                    )
                    logger.info("VoiceSession: tool thinking phrase", {
                        "session_id": session_id, "user_id": user_id,
                        "tool": tool_name,
                    })

        @session.on("session_usage_updated")
        def _on_usage(ev) -> None:  # type: ignore[misc]
            logger.info("VoiceSession: usage updated", {
                "session_id": session_id, "user_id": user_id,
                "usage": str(ev),
            })

        @session.on("error")
        def _on_session_error(ev) -> None:  # type: ignore[misc]
            error = getattr(ev, "error", None) or ev
            logger.error("VoiceSession: AgentSession runtime error", {
                "session_id": session_id, "user_id": user_id,
                "error_type": type(error).__name__,
                "error": str(error),
            })

        @session.on("close")
        def _on_close(ev) -> None:  # type: ignore[misc]
            close_error = getattr(ev, "error", None)
            logger.info("VoiceSession: session close event", {
                "session_id": session_id, "user_id": user_id,
                "error": str(close_error) if close_error else None,
            })
            if close_error:
                error_text = str(close_error).lower()
                code = "session_runtime_failed"
                if "tts" in error_text or "cartesia" in error_text or "audio_output" in error_text:
                    code = "tts_pipeline_failed"
                _log_voice_failure(
                    code=code,
                    user_id=user_id,
                    room_name=ctx.room.name,
                    session_id=session_id,
                    exc=Exception(str(close_error)),
                )
            session_done.set()

        try:
            await session.start(
                room=ctx.room,
                agent=buddy,
                room_options=room_io.RoomOptions(
                    participant_identity=user_id,
                    audio_input=room_io.AudioInputOptions(
                        sample_rate=16000,
                        frame_size_ms=20,
                    ),
                    audio_output=room_io.AudioOutputOptions(
                        sample_rate=24000,  # Cartesia output rate
                    ),
                ),
            )
            await session_done.wait()

            session_end_iso = datetime.now(UTC).isoformat()
            elapsed_ms = int((time.monotonic() - session_start_mono) * 1000)
            asyncio.create_task(
                run_post_session_pipeline(
                    user_id=user_id,
                    session_id=session_id,
                    turns=session_turns,
                    started_at=session_start_iso,
                    ended_at=session_end_iso,
                    duration_ms=elapsed_ms,
                    tool_calls=session_tool_calls,
                ),
                name=f"voice-post-session-{session_id[:8]}",
            )
        except Exception as exc:
            _log_voice_failure(
                code="session_start_failed",
                user_id=user_id,
                room_name=ctx.room.name,
                session_id=session_id,
                exc=exc,
            )
            raise


if __name__ == "__main__":
    logger.info("VoiceWorker: starting", {
        "pid": os.getpid(),
        "livekit_url": settings.LIVEKIT_URL,
        "livekit_configured": settings.livekit_configured,
        "deepgram_configured": bool(settings.DEEPGRAM_API_KEY),
        "cartesia_configured": bool(settings.CARTESIA_API_KEY),
        "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
        "firebase_web_api_key_configured": bool(settings.FIREBASE_WEB_API_KEY),
        "backend_internal_url": settings.BACKEND_INTERNAL_URL,
    })
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=prewarm,
            max_retry=3,
            # Cloud Run injects PORT=8080 and health-checks that port.
            # WorkerOptions defaults to 8081 in prod mode to avoid conflicts, but we make it explicit here to be sure.
            port=int(os.environ.get("PORT", "8081")),
        )
    )
