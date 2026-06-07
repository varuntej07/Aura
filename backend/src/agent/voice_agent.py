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

This module is the thin orchestrator. The pieces it composes live in the
`voice/` package: telemetry, errors, fetchers, prompt context, pipeline
builders, voice conditioning, and the session event recorder.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import UTC, datetime

from livekit.agents import JobContext, JobProcess, WorkerOptions, cli
from livekit.agents import llm as lk_llm
from livekit.agents.voice import room_io
from livekit.plugins import silero

from ..config.settings import settings
from ..lib.logger import logger
from ..services.voice_session_summarizer import run_post_session_pipeline
from .buddy_agent import BuddyAgent
from .voice.auth import mint_firebase_id_token
from .voice.context import gather_session_context
from .voice.pipelines import (
    build_agent_session,
    build_llm_pipeline,
    build_mcp_server,
    build_stt_pipeline,
    build_tts_pipeline,
    build_turn_detector,
)
from .voice.recorder import VoiceSessionRecorder
from .voice.telemetry import log_voice_failure, voice_session_logger
from .voice.voice_controls import derive_voice_controls

# Firebase auto-issued UIDs are 28 alphanumeric chars.
# We refuse anything else so a malformed room name can't drive a session.
_FIREBASE_UID_RE = re.compile(r"^[A-Za-z0-9]{28}$")


def prewarm(process: JobProcess) -> None:
    logger.info("VoiceWorker: prewarming VAD model")
    process.userdata["vad"] = silero.VAD.load()
    # The semantic end-of-turn model can't be prewarmed: LiveKit loads and
    # initializes it inside AgentSession on first use (it needs the job's
    # inference executor, which only exists in the entrypoint). Only its
    # weights are fetched ahead of time, via `download-files` at build time.


async def _connect_to_room(ctx: JobContext, candidate_user_id: str) -> bool:
    """Connect to the LiveKit room. Returns True on success, False on a logged failure."""
    try:
        await asyncio.wait_for(ctx.connect(), timeout=settings.VOICE_CONNECT_TIMEOUT_S)
        return True
    except TimeoutError:
        logger.error("VoiceAgent: room connect timed out", {"room": ctx.room.name})
        log_voice_failure(
            code="room_connect_timeout",
            user_id=candidate_user_id,
            room_name=ctx.room.name,
            session_id=None,
            exc=TimeoutError("LiveKit ctx.connect() timeout"),
        )
        return False
    except Exception as exc:
        logger.exception("VoiceAgent: room connect failed", {
            "room": ctx.room.name, "error": str(exc),
        })
        log_voice_failure(
            code="room_connect_failed",
            user_id=candidate_user_id,
            room_name=ctx.room.name,
            session_id=None,
            exc=exc,
        )
        return False


def _build_sonic3_controls(
    *, session_id: str, user_id: str, dominant_tone: str, dominant_emotion: str
) -> dict:
    """Derive and log the per-session sonic-3 generation controls.

    None kwargs are omitted so a profile-less user constructs the exact default
    voice; only the sonic-3 primary consumes these (the fallbacks are unconditioned).
    """
    voice_speed, voice_emotion = derive_voice_controls(dominant_tone, dominant_emotion)
    sonic3_controls: dict = {}
    if voice_speed is not None:
        sonic3_controls["speed"] = voice_speed
    if voice_emotion is not None:
        sonic3_controls["emotion"] = voice_emotion
    logger.info("VoiceSession: voice controls", {
        "session_id": session_id, "user_id": user_id,
        "speed": voice_speed, "emotion": voice_emotion,
        "source_tone": dominant_tone,
        "source_emotion": dominant_emotion,
    })
    return sonic3_controls


async def entrypoint(ctx: JobContext) -> None:
    logger.info("VoiceAgent: job dispatched", {"room": ctx.room.name})
    candidate_user_id = ctx.room.name.removeprefix("voice-")

    if not await _connect_to_room(ctx, candidate_user_id):
        return

    user_id = candidate_user_id
    if not _FIREBASE_UID_RE.match(user_id):
        logger.error("VoiceAgent: invalid uid in room name", {
            "room": ctx.room.name, "extracted_uid": user_id,
        })
        return

    async with voice_session_logger(user_id, ctx.room.name) as session_id:
        # Fetch profile, memory, last session, archive, aura, and tier in
        # parallel under a hard ceiling. Each source defaults independently.
        session_context = await gather_session_context(user_id, session_id)
        context_vars = session_context.prompt_context_vars

        # Seed the chat history with a system-side note that mirrors what the prompt already says.
        # Keeps the model anchored on the memory across turns without
        # round-tripping to the LLM up front.
        chat_ctx = lk_llm.ChatContext()
        if session_context.memory_summary:
            chat_ctx.add_message(
                role="system",
                content=(
                    "Memory of prior chats with this user:\n"
                    f"{session_context.memory_summary}"
                ),
            )

        # Mint a Firebase ID token so the MCP server can verify the worker.
        # Failure is fatal for tool use so the session can still hold a
        # conversation but tools won't work, so we log loudly and bail.
        try:
            firebase_id_token = await asyncio.wait_for(
                mint_firebase_id_token(user_id),
                timeout=settings.VOICE_TOKEN_MINT_TIMEOUT_S,
            )
        except Exception as exc:
            log_voice_failure(
                code="mcp_token_mint_failed",
                user_id=user_id,
                room_name=ctx.room.name,
                session_id=session_id,
                exc=exc,
            )
            return

        # Per-session voice conditioning from the behavioral profile.
        sonic3_controls = _build_sonic3_controls(
            session_id=session_id,
            user_id=user_id,
            dominant_tone=session_context.dominant_tone,
            dominant_emotion=session_context.dominant_emotion,
        )

        stt_pipeline = build_stt_pipeline()
        llm_pipeline = build_llm_pipeline(user_id)
        tts_pipeline = build_tts_pipeline(sonic3_controls)
        mcp_server = build_mcp_server(firebase_id_token)

        # Loaded inside the entrypoint (not prewarm): the model needs the job's
        # inference executor, which only exists here. If construction fails the
        # session still starts, degrading to VAD-based endpointing.
        try:
            turn_detector = build_turn_detector()
        except Exception as exc:
            turn_detector = None
            logger.warn("VoiceSession: turn detector unavailable — degrading to "
                        "VAD-based endpointing", {
                            "code": "turn_detector_unavailable",
                            "user_id": user_id, "room": ctx.room.name,
                            "session_id": session_id, "error": str(exc),
                        })

        session = build_agent_session(
            stt=stt_pipeline,
            llm=llm_pipeline,
            tts=tts_pipeline,
            vad=ctx.proc.userdata["vad"],
            turn_detector=turn_detector,
            mcp_server=mcp_server,
        )

        buddy = BuddyAgent(
            user_id=user_id,
            context_vars=context_vars,
            chat_ctx=chat_ctx,
        )

        recorder = VoiceSessionRecorder(
            session=session,
            ctx=ctx,
            session_id=session_id,
            user_id=user_id,
            user_tier=session_context.user_tier,
        )
        recorder.attach()

        session_start_iso = datetime.now(UTC).isoformat()
        session_start_mono = time.monotonic()

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
            await recorder.done.wait()

            session_end_iso = datetime.now(UTC).isoformat()
            elapsed_ms = int((time.monotonic() - session_start_mono) * 1000)
            asyncio.create_task(
                run_post_session_pipeline(
                    user_id=user_id,
                    session_id=session_id,
                    turns=recorder.turns,
                    started_at=session_start_iso,
                    ended_at=session_end_iso,
                    duration_ms=elapsed_ms,
                    tool_calls=recorder.tool_calls,
                ),
                name=f"voice-post-session-{session_id[:8]}",
            )
        except Exception as exc:
            log_voice_failure(
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
            # WorkerOptions defaults to 8081 in prod mode to avoid conflicts;
            # we make it explicit here to be sure.
            port=int(os.environ.get("PORT", "8081")),
        )
    )
