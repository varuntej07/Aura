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
import json
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
from ..services.entitlement import add_free_voice_seconds
from ..services.voice_session_summarizer import run_post_session_pipeline
from .buddy_agent import BuddyAgent
from .voice.auth import mint_firebase_id_token
from .voice.context import gather_session_context
from .voice.free_tier_limit import run_free_tier_voice_limit, run_out_of_free_time_close
from .voice.greeting import start_opener_task
from .voice.pipelines import (
    build_agent_session,
    build_llm_pipeline,
    build_mcp_server,
    build_stt_pipeline,
    build_tts_pipeline,
    build_turn_detector,
)
from .voice.recorder import VoiceSessionRecorder
from .voice.revision import worker_revision_fields
from .voice.screen_context import (
    OCR_CONTEXT_TYPE,
    SCREEN_CONTEXT_TYPE,
    TEXT_INPUT_TYPE,
    deliver_screen_context,
    deliver_typed_message,
)
from .voice.screen_frames import SCREEN_FRAME_TOPIC, ScreenFrameStore
from .voice.telemetry import log_voice_failure, voice_session_logger
from .voice.voice_controls import derive_voice_controls
from .voice_prompt import render_screen_sight_note, render_surface_note

# Firebase auto-issued UIDs are 28 alphanumeric chars.
# We refuse anything else so a malformed room name can't drive a session.
_FIREBASE_UID_RE = re.compile(r"^[A-Za-z0-9]{28}$")

# Launch surfaces the client stamps into its participant metadata at /voice/token.
# Anything else (or a missing value) collapses to "app", the neutral default.
_KNOWN_SURFACES = frozenset({"app", "keyboard", "desktop"})
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _resolve_participant_metadata(ctx: JobContext) -> tuple[str | None, str]:
    """Return validated ``(surface, conversation_id)`` from the user's token metadata."""
    try:
        for participant in ctx.room.remote_participants.values():
            raw = (getattr(participant, "metadata", "") or "").strip()
            if not raw:
                continue
            data = json.loads(raw)
            surface = data.get("surface")
            conversation_id = str(data.get("conversation_id") or "").strip()
            return (
                surface if surface in _KNOWN_SURFACES else None,
                conversation_id if _CONVERSATION_ID_RE.fullmatch(conversation_id) else "",
            )
    except Exception:
        pass
    return None, ""


def _resolve_surface(ctx: JobContext) -> str:
    """Read the launch surface ('keyboard' vs 'app') from the user's participant metadata.

    The /voice/token endpoint stamps {"surface": ...} into the token's participant
    metadata; the keyboard sends 'keyboard', the in-app orb sends nothing. We read it
    right after connect (the user is already in the room, since the job is dispatched on
    their join) and default to 'app' on anything unexpected, so a missing or malformed
    value never changes behavior.
    """
    surface, _ = _resolve_participant_metadata(ctx)
    return surface or "app"


def _resolve_followup_metadata(ctx: JobContext) -> tuple[str, str | None, list[str]]:
    """Read optional notification lineage stamped by the token endpoint."""
    try:
        for participant in ctx.room.remote_participants.values():
            raw = (getattr(participant, "metadata", "") or "").strip()
            if not raw:
                continue
            data = json.loads(raw)
            if data.get("origin") != "notification_tap":
                return "organic", None, []
            candidate_id = str(data.get("origin_candidate_id") or "").strip()[:80]
            lineage = data.get("lineage_chain")
            return (
                "notification_tap",
                candidate_id or None,
                [str(value).strip()[:80] for value in lineage if str(value).strip()][:20]
                if isinstance(lineage, list)
                else [],
            )
    except Exception:
        pass
    return "organic", None, []


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

    followup_session_id: str | None = None
    if settings.FOLLOWUP_SHADOW or settings.PROACTIVE_FOLLOWUP_SEND:
        from ..services.session_followup.lifecycle import session_lifecycle_service

        origin, origin_candidate_id, lineage_chain = _resolve_followup_metadata(ctx)
        followup_session_id = await session_lifecycle_service.start_session(
            user_id,
            None,
            surface="voice",
            origin=origin,
            origin_candidate_id=origin_candidate_id,
            lineage_chain=lineage_chain,
        )

    async with voice_session_logger(
        user_id,
        ctx.room.name,
        session_id=followup_session_id,
    ) as session_id:
        # Fetch profile, memory, last session, archive, aura, and tier in
        # parallel under a hard ceiling. Each source defaults independently.
        session_context = await gather_session_context(user_id, session_id)
        context_vars = session_context.prompt_context_vars

        # Memory-seeded opener, raced against the static greeting: it runs in
        # parallel with the pipeline build below, and on_enter waits at most
        # VOICE_GREETING_SEED_BUDGET_S for it before falling back to a static
        # casual line (sub-1s first-audio feel preserved).
        opener_task = start_opener_task(
            session_context, session_id=session_id, user_id=user_id
        )

        # Where the call was launched from. Baked into the prompt once here (the prompt is
        # built once per session in BuddyAgent), so a keyboard tap stays short and
        # task-focused for the whole session, not just the first turn.
        persisted_surface, conversation_id = _resolve_participant_metadata(ctx)
        surface = persisted_surface or "app"
        context_vars["surface"] = render_surface_note(surface)
        context_vars["screen_sight"] = render_screen_sight_note(surface)
        if persisted_surface is None:
            logger.warn("voice_run_missing_surface", {
                "session_id": session_id, "user_id": user_id,
            })
        if not conversation_id:
            logger.warn("voice_run_missing_conversation_id", {
                "session_id": session_id, "user_id": user_id,
            })
        if surface != "app":
            logger.info("VoiceSession: launch surface", {
                "session_id": session_id, "user_id": user_id, "surface": surface,
            })

        # memory_summary is already injected once via the {memory_summary} slot in
        # VOICE_PROMPT (see context.prompt_context_vars). We deliberately do NOT add a
        # second system-role copy here: a duplicate both wastes prompt tokens and can
        # contradict the live slots (e.g. a stale "timezone: PST" memory vs the live
        # {timezone}). The empty ChatContext is still passed so BuddyAgent owns its history.
        chat_ctx = lk_llm.ChatContext()

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
            logger.warn("VoiceSession: turn detector unavailable, degrading to "
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

        # Screen frames from the desktop overlay (armed screen sight). Registered
        # BEFORE session.start so a frame racing the pipeline build is assembled,
        # not dropped. Costs nothing on sessions that never send one; the byte
        # stream can only carry frames from this room's participant.
        screen_frames = ScreenFrameStore(session_id=session_id, user_id=user_id)
        try:
            ctx.room.register_byte_stream_handler(
                SCREEN_FRAME_TOPIC, screen_frames.handle_stream
            )
        except Exception as exc:
            logger.warn("VoiceSession: screen frame handler registration failed", {
                "session_id": session_id, "user_id": user_id, "error": str(exc),
            })

        # "there" is fetch_user_profile's no-name fallback (see voice/context.py),
        # not a real name; Buddy Drafts must never sign an email with it.
        draft_display_name = context_vars.get("name", "")
        if draft_display_name.strip().lower() == "there":
            draft_display_name = ""

        buddy = BuddyAgent(
            user_id=user_id,
            context_vars=context_vars,
            chat_ctx=chat_ctx,
            screen_frames=screen_frames,
            session_id=session_id,
            user_tier=session_context.user_tier,
            display_name=draft_display_name,
            launch_surface=surface,
            opener_task=opener_task,
        )

        recorder = VoiceSessionRecorder(
            session=session,
            ctx=ctx,
            session_id=session_id,
            user_id=user_id,
            user_tier=session_context.user_tier,
            tool_observer=buddy,
            screen_frames=screen_frames,
        )
        recorder.attach()

        session_start_iso = datetime.now(UTC).isoformat()
        session_start_mono = time.monotonic()

        # Free-tier voice budget task (warn at T-60s, wind down and close at the
        # cap), armed after start, cancelled on session end.
        voice_limit_task: asyncio.Task | None = None

        # On-screen / field context handed in over the data channel: the keyboard's
        # "talk about what's on my screen", an OCR snapshot, or a typed message. The
        # handler is registered BEFORE session.start so a packet that lands while the
        # pipelines are still building is buffered, not dropped; it is flushed once the
        # session is live. screen_context fires once per session.
        screen_context_fired = False
        session_live = False
        pending_context_payloads: list[dict] = []
        context_tasks: list[asyncio.Task] = []

        def _dispatch_context_payload(msg: dict) -> None:
            nonlocal screen_context_fired
            msg_type = msg.get("type")
            if msg_type == SCREEN_CONTEXT_TYPE:
                if screen_context_fired:
                    return
                screen_context_fired = True
                context_tasks.append(asyncio.create_task(
                    deliver_screen_context(
                        session,
                        context_before=str(msg.get("context_before", "")),
                        field_type=msg.get("field_type"),
                        app=msg.get("app"),
                        session_id=session_id,
                        user_id=user_id,
                    ),
                    name=f"voice-screen-ctx-{session_id[:8]}",
                ))
            elif msg_type == OCR_CONTEXT_TYPE:
                context_tasks.append(asyncio.create_task(
                    deliver_screen_context(
                        session,
                        context_before=str(msg.get("text", "")),
                        field_type=None,
                        app=None,
                        session_id=session_id,
                        user_id=user_id,
                    ),
                    name=f"voice-ocr-ctx-{session_id[:8]}",
                ))
            elif msg_type == TEXT_INPUT_TYPE:
                context_tasks.append(asyncio.create_task(
                    deliver_typed_message(
                        session,
                        text=str(msg.get("text", "")),
                        session_id=session_id,
                        user_id=user_id,
                    ),
                    name=f"voice-text-input-{session_id[:8]}",
                ))

        def _on_data_received(packet) -> None:
            try:
                raw = getattr(packet, "data", None)
                if not raw:
                    return
                msg = json.loads(bytes(raw).decode("utf-8"))
            except Exception:
                return  # not our JSON; ignore (other features may share the channel)
            if not isinstance(msg, dict) or "type" not in msg:
                return
            if session_live:
                _dispatch_context_payload(msg)
            else:
                pending_context_payloads.append(msg)

        ctx.room.on("data_received", _on_data_received)

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

            # The session is live: process any context packet that arrived during startup,
            # then let the handler dispatch live ones directly.
            session_live = True
            for _payload in pending_context_payloads:
                _dispatch_context_payload(_payload)
            pending_context_payloads.clear()

            # Free tier only: warn ~60s before the daily voice budget runs out,
            # then wind the call down at the cap (enforced). A caller who is
            # already out of budget gets Buddy's greeting, one out-of-time line,
            # and a graceful close instead of a full session. None = the budget
            # read failed, which disables enforcement (degrade, never wrongly cut).
            if session_context.user_tier == "free":
                remaining = session_context.remaining_free_voice_seconds
                if remaining is not None and remaining <= 0:
                    voice_limit_task = asyncio.create_task(
                        run_out_of_free_time_close(
                            session,
                            ctx,
                            session_id=session_id,
                            user_id=user_id,
                        ),
                        name=f"voice-free-limit-{session_id[:8]}",
                    )
                else:
                    voice_limit_task = asyncio.create_task(
                        run_free_tier_voice_limit(
                            session,
                            ctx,
                            remaining_seconds=remaining,
                            session_id=session_id,
                            user_id=user_id,
                        ),
                        name=f"voice-free-limit-{session_id[:8]}",
                    )

            await recorder.done.wait()

            session_end_iso = datetime.now(UTC).isoformat()
            elapsed_ms = int((time.monotonic() - session_start_mono) * 1000)

            # Free tier only: bank this call's seconds against today's voice budget so the
            # per-day total carries across calls. Fire-and-forget; never blocks teardown.
            if session_context.user_tier == "free":
                asyncio.create_task(
                    add_free_voice_seconds(user_id, elapsed_ms // 1000),
                    name=f"voice-budget-write-{session_id[:8]}",
                )

            asyncio.create_task(
                run_post_session_pipeline(
                    user_id=user_id,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    surface=persisted_surface or "unknown",
                    turns=recorder.turns,
                    started_at=session_start_iso,
                    ended_at=session_end_iso,
                    duration_ms=elapsed_ms,
                    tool_calls=recorder.tool_calls,
                    action_receipts=recorder.action_receipts,
                    screen_sight_frame_count=screen_frames.frame_count,
                ),
                name=f"voice-post-session-{session_id[:8]}",
            )
            if settings.FOLLOWUP_SHADOW or settings.PROACTIVE_FOLLOWUP_SEND:
                from ..services.session_followup.lifecycle import session_lifecycle_service

                asyncio.create_task(
                    session_lifecycle_service.note_voice_disconnect(
                        user_id,
                        session_id,
                    ),
                    name=f"followup-voice-grace-{session_id[:8]}",
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
        finally:
            if voice_limit_task is not None:
                voice_limit_task.cancel()
            for task in context_tasks:
                task.cancel()


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
        **worker_revision_fields(),
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
