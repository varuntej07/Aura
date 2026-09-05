"""
LiveKit voice agent using cascading architecture: STT -> LLM -> TTS

Pipeline plugins:
  Deepgram Nova STT (with nova-3 -> nova-2 fallback)
  Anthropic Claude LLM (with Gemini Flash fallback)
  Cartesia TTS (Sonic 3.5 -> Deepgram Aura 2 -> Sonic 3 fallback)
  Silero VAD + LiveKit audio turn detector (inference.TurnDetector)

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
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from livekit.agents import JobContext, JobProcess, WorkerOptions, cli, inference
from livekit.agents import llm as lk_llm
from livekit.agents.voice import room_io

from ..config.settings import settings
from ..lib.logger import logger
from ..services.analytics.arize_tracing import (
    bind_arize_context,
    configure_arize_tracing,
    flush_arize_tracing,
    reset_arize_context,
)
from ..services.entitlement import add_free_voice_seconds
from ..services.voice_session_summarizer import run_post_session_pipeline
from .buddy_agent import BuddyAgent
from .voice.artifact_delivery import (
    ARTIFACT_DISPLAYED_TYPE,
    ArtifactDeliveryTracker,
)
from .voice.auth import mint_firebase_id_token
from .voice.bridge_handover import BRIDGE_CONTROL_TYPES, BridgeHandoverCoordinator
from .voice.context import gather_session_context
from .voice.free_tier_limit import run_free_tier_voice_limit, run_out_of_free_time_close
from .voice.guide_default_profile import GenericGuideProfile
from .voice.guide_mode import (
    GUIDE_HEARTBEAT_TYPE,
    GUIDE_MODE_TYPE,
    GuideCoordinator,
)
from .voice.guide_provider_adapter import AuraGuideDecisionProvider
from .voice.guide_task_runtime import GuideTaskRuntime
from .voice.input_liveness import InputLiveness, watch_input_liveness
from .voice.interview import (
    INTERVIEW_MATERIAL_TOPIC,
    MATERIAL_OVERLAY_SHOWN_TYPE,
    InterviewMaterialStore,
    buddy_owns_conversation,
)
from .voice.output_mode import (
    DEFAULT_OUTPUT_MODE,
    KNOWN_OUTPUT_MODES,
    OUTPUT_MODE_TYPE,
    OutputModeController,
)
from .voice.pipelines import (
    build_agent_session,
    build_llm_pipeline,
    build_mcp_server,
    build_noise_cancellation,
    build_stt_pipeline,
    build_tts_pipeline,
    build_turn_detector,
    cartesia_tts_legs,
    describe_llm_fallback_legs,
)
from .voice.recorder import VoiceSessionRecorder
from .voice.revision import worker_revision_fields
from .voice.screen_context_control import SCREEN_CONTEXT_UNAVAILABLE_TYPE
from .voice.screen_context import (
    CLIENT_EVENTS_TOPIC,
    OCR_CONTEXT_TYPE,
    SCREEN_CONTEXT_TYPE,
    TEXT_INPUT_TYPE,
    TypedMessageQueue,
    deliver_screen_context,
)
from .voice.screen_context_stream import SCREEN_CONTEXT_TOPIC, StructuredContextStore
from .voice.spoken_language import SpokenLanguageFollower
from .voice.screen_frames import SCREEN_FRAME_TOPIC, ScreenFrameStore
from .voice.telemetry import log_voice_failure, voice_session_logger
from .voice.turn_metrics import VoiceTurnMetrics
from .voice.voice_catalog import (
    REASON_TIER_LOCKED,
    REASON_UNKNOWN,
    BuddyVoice,
    resolve_voice,
)
from .voice.voice_controls import derive_voice_controls

# Firebase auto-issued UIDs are 28 alphanumeric chars.
# We refuse anything else so a malformed room name can't drive a session.
_FIREBASE_UID_RE = re.compile(r"^[A-Za-z0-9]{28}$")

# Launch surfaces the client stamps into its participant metadata at /voice/token.
# Anything else (or a missing value) collapses to "app", the neutral default.
_KNOWN_SURFACES = frozenset({"app", "keyboard", "desktop"})
_KNOWN_VOICE_MODES = frozenset({"standard", "guide", "onboarding"})
_KNOWN_CLIENT_PLATFORMS = frozenset({"android", "ios", "windows", "macos"})
_APP_VERSION_RE = re.compile(r"^\d+(?:\.\d+){0,3}$")
_ARTIFACT_ACK_CAPABILITY = "displayed-v1"
_CONVERSATION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


@dataclass(frozen=True, slots=True)
class LaunchMetadata:
    """Everything the client stamped into its participant token, validated once.

    This used to be nine ``_resolve_*`` functions with a byte-identical body:
    walk ``remote_participants``, strip ``participant.metadata``, ``json.loads``
    it, read one key, validate, swallow every exception, return a default. The
    entrypoint called seven of them back to back, so one JSON document was
    parsed seven times and seven independent ``except: pass`` blocks could each
    hide a malformed blob without anyone noticing.

    Every field keeps the default its own resolver had, so a client that sends
    nothing, or sends nonsense, lands exactly where it did before.
    """

    surface: str | None = None
    conversation_id: str = ""
    voice_mode: str = "standard"
    client_platform: str = ""
    app_version: str = ""
    bridged: bool = False
    output_mode: str = DEFAULT_OUTPUT_MODE
    artifact_ack_capable: bool = False
    voice_request_id: str = ""
    voice_requested_at_ms: int | None = None
    origin: str = "organic"
    origin_candidate_id: str | None = None
    lineage_chain: tuple[str, ...] = ()


def _first_participant_metadata(ctx: JobContext) -> dict:
    """The first remote participant's token metadata, or {}.

    ``ctx.connect()`` connects the AGENT. This is empty until the USER joins,
    which is why every caller must sit behind ``_wait_for_user_participant``.
    """
    try:
        for participant in ctx.room.remote_participants.values():
            raw = (getattr(participant, "metadata", "") or "").strip()
            if not raw:
                continue
            data = json.loads(raw)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}
    return {}


def parse_launch_metadata(ctx: JobContext) -> LaunchMetadata:
    """Read and validate the whole launch token in one pass."""
    data = _first_participant_metadata(ctx)
    if not data:
        return LaunchMetadata()

    surface = data.get("surface")
    conversation_id = str(data.get("conversation_id") or "").strip()
    mode = data.get("mode")
    platform = str(data.get("platform") or "").casefold()
    app_version = str(data.get("app_version") or "").strip()
    output_mode = data.get("output_mode")
    requested_at = data.get("voice_requested_at_ms")

    origin, candidate_id, lineage = "organic", None, ()
    if data.get("origin") == "notification_tap":
        raw_lineage = data.get("lineage_chain")
        origin = "notification_tap"
        candidate_id = str(data.get("origin_candidate_id") or "").strip()[:80] or None
        lineage = (
            tuple(
                str(value).strip()[:80]
                for value in raw_lineage
                if str(value).strip()
            )[:20]
            if isinstance(raw_lineage, list)
            else ()
        )

    return LaunchMetadata(
        surface=surface if surface in _KNOWN_SURFACES else None,
        conversation_id=(
            conversation_id
            if _CONVERSATION_ID_RE.fullmatch(conversation_id)
            else ""
        ),
        voice_mode=mode if mode in _KNOWN_VOICE_MODES else "standard",
        client_platform=platform if platform in _KNOWN_CLIENT_PLATFORMS else "",
        app_version=app_version if _APP_VERSION_RE.fullmatch(app_version) else "",
        bridged=data.get("bridged") is True,
        output_mode=(
            output_mode if output_mode in KNOWN_OUTPUT_MODES else DEFAULT_OUTPUT_MODE
        ),
        artifact_ack_capable=data.get("artifact_ack") == _ARTIFACT_ACK_CAPABILITY,
        voice_request_id=str(data.get("voice_request_id") or "")[:64],
        voice_requested_at_ms=(
            requested_at if isinstance(requested_at, int) else None
        ),
        origin=origin,
        origin_candidate_id=candidate_id,
        lineage_chain=lineage,
    )


def _resolve_participant_metadata(ctx: JobContext) -> tuple[str | None, str]:
    """Validated ``(surface, conversation_id)``. Kept as the narrow read used by
    the identity contract test; everything else goes through
    :func:`parse_launch_metadata`."""
    launch = parse_launch_metadata(ctx)
    return launch.surface, launch.conversation_id


def _resolve_voice_mode(ctx: JobContext) -> str:
    """The bounded voice-session mode stamped by the token endpoint."""
    return parse_launch_metadata(ctx).voice_mode


def _resolve_bridged(ctx: JobContext) -> bool:
    """True when the desktop stamped ``bridged``, meaning it opened a Realtime leg
    and this worker should HOLD for a handover instead of greeting."""
    return parse_launch_metadata(ctx).bridged


def prewarm(process: JobProcess) -> None:
    # Prewarm runs inside each LiveKit job process. Configure tracing here so
    # its provider exists in the same process that creates agent spans.
    configure_arize_tracing("voice")
    from ..shared.tool_exposure import verify_core_tool_exposure

    verify_core_tool_exposure(component="voice")
    logger.info("VoiceWorker: prewarming VAD model")
    # Bundled local silero VAD (livekit-local-inference); replaces the deprecated
    # livekit-plugins-silero. Loaded here so the model isn't cold on the first job.
    process.userdata["vad"] = inference.VAD(model="silero")
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
        logger.exception(
            "VoiceAgent: room connect failed",
            {
                "room": ctx.room.name,
                "error": str(exc),
            },
        )
        log_voice_failure(
            code="room_connect_failed",
            user_id=candidate_user_id,
            room_name=ctx.room.name,
            session_id=None,
            exc=exc,
        )
        return False


async def _wait_for_user_participant(ctx: JobContext, user_id: str) -> bool:
    """Block until the user's participant is in the room. False means they never came.

    ``ctx.connect()`` connects the AGENT, nothing more. Every launch parameter this
    worker reads (surface, conversation_id, bridged, output_mode, voice_request_id) lives
    in the USER's participant token metadata, which ``parse_launch_metadata`` reads from
    ``ctx.room.remote_participants`` and which quietly returns its defaults when that
    dict is still empty. The old code asserted in a comment that connect implied the
    participant existed; it does not, and the resulting doc reads
    ``surface: "unknown", conversation_id: ""`` with no error anywhere.

    Returns immediately when the participant already joined, which is the common case.
    """
    try:
        await asyncio.wait_for(
            ctx.wait_for_participant(identity=user_id),
            timeout=settings.VOICE_PARTICIPANT_WAIT_S,
        )
        return True
    except TimeoutError:
        # Nobody to serve. Holding the room open just buys a 5-minute empty session.
        logger.error(
            "voice_run_participant_never_joined",
            {
                "room": ctx.room.name,
                "user_id": user_id,
                "waited_s": settings.VOICE_PARTICIPANT_WAIT_S,
            },
        )
        log_voice_failure(
            code="participant_never_joined",
            user_id=user_id,
            room_name=ctx.room.name,
            session_id=None,
            exc=TimeoutError("user participant never joined"),
        )
        return False
    except Exception as exc:
        logger.exception(
            "VoiceAgent: participant wait failed",
            {"room": ctx.room.name, "user_id": user_id, "error": str(exc)},
        )
        return False


def _build_sonic3_controls(
    *, session_id: str, user_id: str, dominant_tone: str, dominant_emotion: str
) -> dict:
    """Derive and log per-session Cartesia Sonic generation controls.

    None kwargs are omitted so a profile-less user constructs the exact default
    voice; only the Sonic 3.5 primary consumes these (the fallbacks are unconditioned).
    """
    voice_speed, voice_emotion = derive_voice_controls(dominant_tone, dominant_emotion)
    sonic3_controls: dict = {}
    if voice_speed is not None:
        sonic3_controls["speed"] = voice_speed
    if voice_emotion is not None:
        sonic3_controls["emotion"] = voice_emotion
    logger.info(
        "VoiceSession: voice controls",
        {
            "session_id": session_id,
            "user_id": user_id,
            "speed": voice_speed,
            "emotion": voice_emotion,
            "source_tone": dominant_tone,
            "source_emotion": dominant_emotion,
        },
    )
    return sonic3_controls


def _resolve_buddy_voice(
    *, session_id: str, user_id: str, voice_id: str, user_tier: str
) -> BuddyVoice:
    """Resolve the user's stored voice slug, logging every fallback path.

    A rejected slug or a tier-locked voice is logged at warn: the user picked a
    voice and is not getting it, which must never look identical to a healthy
    resolve. An unset slug is the normal state for everyone who has not picked
    and stays at info.
    """
    voice, reason = resolve_voice(voice_id, user_tier)
    fields = {
        "session_id": session_id,
        "user_id": user_id,
        "requested": voice_id,
        "resolved": voice.slug,
        "user_tier": user_tier,
        "reason": reason,
    }
    if reason in (REASON_UNKNOWN, REASON_TIER_LOCKED):
        logger.warn("VoiceSession: voice pick not honored, using default", fields)
    else:
        logger.info("VoiceSession: voice resolved", fields)
    return voice


async def entrypoint(ctx: JobContext) -> None:
    worker_started_mono = time.monotonic()
    logger.info("VoiceAgent: job dispatched", {"room": ctx.room.name})
    candidate_user_id = ctx.room.name.removeprefix("voice-")

    if not await _connect_to_room(ctx, candidate_user_id):
        return

    user_id = candidate_user_id
    if not _FIREBASE_UID_RE.match(user_id):
        logger.error(
            "VoiceAgent: invalid uid in room name",
            {
                "room": ctx.room.name,
                "extracted_uid": user_id,
            },
        )
        return

    # Before ANY participant metadata is read. Everything below this line depends on
    # the user's participant actually being in the room.
    if not await _wait_for_user_participant(ctx, user_id):
        return

    from ..services.session_followup.lifecycle import session_lifecycle_service

    # Parsed ONCE, here, and read from everywhere below. Safe at this point and
    # not before: _wait_for_user_participant has returned, so the participant
    # carrying this metadata is genuinely in the room. An empty
    # remote_participants map reads as "client sent nothing" and is
    # indistinguishable from a client that sent everything.
    launch = parse_launch_metadata(ctx)
    followup_session_id: str | None = await session_lifecycle_service.start_session(
        user_id,
        None,
        surface="voice",
        origin=launch.origin,
        origin_candidate_id=launch.origin_candidate_id,
        lineage_chain=list(launch.lineage_chain),
    )

    async with voice_session_logger(
        user_id,
        ctx.room.name,
        session_id=followup_session_id,
    ) as session_id:
        # Where the call was launched from. Baked into the prompt once here (the prompt is
        # built once per session in BuddyAgent), so a keyboard tap stays short and
        # task-focused for the whole session, not just the first turn.
        # conversation_id is read BEFORE the context fetch: it is what lets the
        # fetch pick up the text exchanges this same conversation typed just
        # before starting the call.
        persisted_surface = launch.surface
        conversation_id = launch.conversation_id

        # Fetch profile, memory, last session, archive, aura, and tier in
        # parallel under a hard ceiling. Each source defaults independently.
        session_context = await gather_session_context(user_id, session_id, conversation_id)
        context_vars = session_context.prompt_context_vars

        voice_request_id = launch.voice_request_id
        voice_requested_at_ms = launch.voice_requested_at_ms
        surface = persisted_surface or "app"
        client_platform = launch.client_platform
        app_version = launch.app_version
        voice_mode = launch.voice_mode
        # Realtime-bridge session: the API only stamps signed `bridged` participant
        # metadata after it has admitted the Realtime leg. Treat that metadata as the
        # single handover authority so the separately deployed API and LiveKit worker
        # cannot disagree because one runtime missed an environment update.
        bridged = launch.bridged
        # Output mute is audio-only suppression, and the Realtime bridge plays
        # through the desktop's own <audio> element rather than a LiveKit track,
        # so a bridged text-mode session would speak straight past the mute. The
        # token endpoint already refuses to stamp `bridged` in text mode; this is
        # the worker-side half of the same rule.
        output_mode = launch.output_mode
        artifact_ack_capable = launch.artifact_ack_capable
        if output_mode == "text" and bridged:
            bridged = False
            logger.info(
                "bridge: refused, output mode is text",
                {"session_id": session_id, "user_id": user_id},
            )
        logger.info(
            "bridge: mode resolved",
            {
                "session_id": session_id,
                "bridged": bridged,
            },
        )
        if persisted_surface is None:
            logger.warn(
                "voice_run_missing_surface",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                },
            )
        if not conversation_id:
            # Mint one rather than run without a thread. Transcript reconciliation is
            # gated on a non-empty conversation_id (voice_session_summarizer), so a run
            # without one keeps its turns in raw_turns and they never reach the user's
            # chat history. Losing the transcript of a call that happened is worse than
            # creating a thread the client did not ask for.
            conversation_id = f"voice-{session_id}"[:128]
            logger.warn(
                "voice_run_missing_conversation_id",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "minted_conversation_id": conversation_id,
                },
            )
        if surface != "app":
            logger.info(
                "VoiceSession: launch surface",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "surface": surface,
                },
            )
        if voice_mode == "guide":
            logger.info(
                "VoiceSession: Guide Mode launch",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                },
            )

        # Session context is injected once in BuddyAgent's final context block. We
        # deliberately do NOT add a second system-role copy here: a duplicate both
        # wastes prompt tokens and can contradict the live values. The empty
        # ChatContext is still passed so BuddyAgent owns its history.
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
        buddy_voice = _resolve_buddy_voice(
            session_id=session_id,
            user_id=user_id,
            voice_id=session_context.profile.get("voice_id", ""),
            user_tier=session_context.user_tier,
        )

        stt_pipeline = build_stt_pipeline()
        llm_pipeline = build_llm_pipeline(user_id)
        turn_metrics = VoiceTurnMetrics(
            session_id=session_id,
            fallback_legs=describe_llm_fallback_legs(llm_pipeline),
            openai_api_key_present=bool(settings.OPENAI_API_KEY),
        )
        tts_pipeline = build_tts_pipeline(
            sonic3_controls,
            cartesia_voice=buddy_voice.cartesia_voice_id,
            deepgram_model=buddy_voice.deepgram_model,
        )
        mcp_server = build_mcp_server(firebase_id_token, session_id)

        # Built inside the entrypoint (not prewarm): the detector binds to the
        # job's inference executor, which only exists here. If construction
        # fails the session still starts, degrading to VAD-based endpointing —
        # build_agent_session() omits the turn_detection key in that case rather
        # than passing None, which would disable end-of-turn detection outright.
        #
        # This is now the audio detector, so there is no local model load and no
        # HuggingFace fetch to fail; the v1 -> v1-mini fallback is handled inside
        # LiveKit. `turn_detector_unavailable` has not fired in production.
        try:
            turn_detector = build_turn_detector()
        except Exception as exc:
            turn_detector = None
            logger.warn(
                "VoiceSession: turn detector unavailable, degrading to VAD-based endpointing",
                {
                    "code": "turn_detector_unavailable",
                    "user_id": user_id,
                    "room": ctx.room.name,
                    "session_id": session_id,
                    "error": str(exc),
                },
            )

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
            ctx.room.register_byte_stream_handler(SCREEN_FRAME_TOPIC, screen_frames.handle_stream)
        except Exception as exc:
            logger.warn(
                "VoiceSession: screen frame handler registration failed",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )

        # Structured UI Automation context from the desktop overlay, on its own
        # byte-stream topic. Same registration rule as frames above: before
        # session.start, because the desktop captures it while the user is still
        # speaking and the whole latency win depends on it landing early.
        screen_context = StructuredContextStore(session_id=session_id, user_id=user_id)
        try:
            ctx.room.register_byte_stream_handler(
                SCREEN_CONTEXT_TOPIC, screen_context.handle_stream
            )
        except Exception as exc:
            logger.warn(
                "VoiceSession: screen context handler registration failed",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )

        # Interview materials (a pasted job description) arrive on their own
        # byte-stream topic. Registered before session.start for the same reason
        # as the two above: a stream racing the pipeline build must be assembled,
        # not dropped. The text lands in session userdata and nowhere else.
        interview_materials = InterviewMaterialStore(
            session_id=session_id,
            user_id=user_id,
            client_events_topic=CLIENT_EVENTS_TOPIC,
        )
        try:
            ctx.room.register_byte_stream_handler(
                INTERVIEW_MATERIAL_TOPIC, interview_materials.handle_stream
            )
        except Exception as exc:
            logger.warn(
                "VoiceSession: interview material handler registration failed",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "error": str(exc),
                },
            )

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
            screen_context=screen_context,
            session_id=session_id,
            user_tier=session_context.user_tier,
            display_name=draft_display_name,
            launch_surface=surface,
            client_platform=client_platform,
            app_version=app_version,
            voice_mode=voice_mode,
            connector_states=session_context.connector_states,
            bridged=bridged,
            text_output=output_mode == "text",
            turn_metrics=turn_metrics,
            firebase_id_token=firebase_id_token,
        )

        async def _resume_buddy(
            return_ctx: lk_llm.ChatContext,
        ) -> BuddyAgent:
            # The epoch is read at the moment the return is requested and carried
            # into on_enter, which is where IDLE is actually committed.
            await buddy.prepare_interview_resume(
                return_ctx, session.userdata.interview.ownership_epoch
            )
            return buddy

        session.userdata.buddy_factory = _resume_buddy
        session.userdata.materials = interview_materials
        # A stalled byte-stream reader would otherwise outlive the session that
        # armed it, holding the bytes it had already accumulated.
        ctx.add_shutdown_callback(interview_materials.aclose)

        bridge = (
            BridgeHandoverCoordinator(
                session=session,
                buddy=buddy,
                room=ctx.room,
                session_id=session_id,
                user_id=user_id,
            )
            if bridged
            else None
        )

        guide_runtime = GuideTaskRuntime(
            user_id=user_id,
            voice_session_id=session_id,
            screen_frames=screen_frames,
            room=ctx.room,
            session=session,
            # Application-neutral by design: Guide adapts to whatever task the
            # user asks by trusting the planner's own steps, rather than following
            # a script written per application. GenericGuideProfile is the only
            # GuideTaskProfile implementation.
            profile=GenericGuideProfile(),
            decision_provider=AuraGuideDecisionProvider(),
        )
        logger.info(
            "GuideTrace",
            {
                "session_id": session_id,
                "user_id": user_id,
                **guide_runtime.diagnostic_state(),
                "stage": "execution",
                "outcome": "succeeded",
                "reason": "guide_runtime_configured",
            },
        )
        guide = GuideCoordinator(
            session=session,
            buddy=buddy,
            room=ctx.room,
            session_id=session_id,
            user_id=user_id,
            task_runtime=guide_runtime,
            screen_frames=screen_frames,
            display_name=context_vars.get("name") or "there",
        )
        screen_frames.set_frame_listener(guide.submit_frame)
        # Injects a structured snapshot into Buddy's persistent context the moment
        # it assembles, which is normally while the user is still speaking. That
        # is what lets a screen-aware turn keep its speculative reply instead of
        # paying a cold round trip (see voice/speculation.py).
        def _route_structured_context(context) -> None:
            if buddy_owns_conversation(session):
                buddy.ingest_structured_context(context)

        screen_context.set_context_listener(_route_structured_context)
        # Same idea for graph memory, on a different trigger. Retrieval used to
        # run inside on_user_turn_completed, where it was serial silence the
        # user sat through; the 2026-08-01 baseline measured it timing out on
        # 15 of 15 turns and returning memory on none of them. Starting it off
        # the first substantial interim transcript overlaps it with their own
        # speech, which both hides the cost and makes a realistic budget
        # affordable. The recorder listens to this same event for its own
        # reasons, which is why this is `on` and not an exclusive setter.
        session.on(
            "user_input_transcribed",
            lambda ev: (
                buddy.ingest_partial_transcript(ev.transcript, ev.is_final)
                if buddy_owns_conversation(session)
                else None
            ),
        )

        liveness = InputLiveness()
        recorder = VoiceSessionRecorder(
            session=session,
            ctx=ctx,
            session_id=session_id,
            user_id=user_id,
            user_tier=session_context.user_tier,
            tool_observer=buddy,
            screen_frames=screen_frames,
            guide=guide,
            worker_started_monotonic=worker_started_mono,
            voice_requested_at_ms=voice_requested_at_ms,
            voice_request_id=voice_request_id,
            surface=surface,
            turn_metrics=turn_metrics,
            liveness=liveness,
        )
        buddy.bind_direct_action_recorder(recorder.record_direct_action)
        # The recorder is the one owner of a server-side close, so a session
        # Guide gives up on is stamped with a reason like any other.
        guide_runtime.bind_session_closer(recorder.close_session)
        recorder.attach()
        recorder.watch_llm_fallback(llm_pipeline)

        # Match Buddy's speaking language to the language the user is actually
        # speaking. STT runs in Deepgram's `multi` code-switching mode, so the
        # detected language arrives on the transcript event; without this the
        # Cartesia legs keep the plugin's "en" default and a Spanish reply is
        # pronounced as English. Registered as its own listener rather than folded
        # into the recorder: the recorder records, and this retunes an output.
        spoken_language = SpokenLanguageFollower(
            cartesia_legs=cartesia_tts_legs(tts_pipeline),
            session_id=session_id,
            user_id=user_id,
        )
        session.on("user_input_transcribed", spoken_language.note_transcript)

        session_start_iso = datetime.now(UTC).isoformat()
        session_start_mono = time.monotonic()

        # Free-tier voice budget task (warn at T-60s, wind down and close at the
        # cap), armed after start, cancelled on session end.
        voice_limit_task: asyncio.Task | None = None
        # Inbound-audio watchdog, armed after start, cancelled on session end.
        liveness_task: asyncio.Task | None = None

        # On-screen / field context handed in over the data channel: the keyboard's
        # "talk about what's on my screen", an OCR snapshot, or a typed message. The
        # handler is registered BEFORE session.start so a packet that lands while the
        # pipelines are still building is buffered, not dropped; it is flushed once the
        # session is live. screen_context fires once per session.
        screen_context_fired = False
        session_live = False
        pending_context_payloads: list[tuple[dict, str, str]] = []
        context_tasks: list[asyncio.Task] = []
        typed_messages = TypedMessageQueue(
            session=session,
            room=ctx.room,
            session_id=session_id,
            user_id=user_id,
            bind_text_observer=buddy.bind_typed_text_observer,
        )
        output_mode_controller = OutputModeController(
            session=session,
            room=ctx.room,
            buddy=buddy,
            session_id=session_id,
            user_id=user_id,
            initial_mode=output_mode,
            client_events_topic=CLIENT_EVENTS_TOPIC,
        )
        # Capable desktop builds must prove the first card. Older builds omit the
        # capability and keep the legacy optimistic behavior.
        artifact_delivery = ArtifactDeliveryTracker(
            session_id=session_id,
            user_id=user_id,
            client_events_topic=CLIENT_EVENTS_TOPIC,
            client_ack_capable=artifact_ack_capable,
        )
        buddy.bind_artifact_delivery(artifact_delivery)

        def _ambient_context_suspended(msg_type: str) -> bool:
            """Whether a specialized agent owns the conversation.

            Screen and OCR context are AMBIENT: nobody asked for them this turn,
            and they arrive as a generate_reply that would land on the intake task
            mid-question, answering a screenshot instead of "which company".

            Typed messages are deliberately NOT suspended here. Those are the
            user's own words, and they reach whichever interview agent is active,
            whose own tools (cancel_setup, end_mock_interview) are the right owner
            for them. Suppressing those would leave a user on a muted mic with no
            way out of Interview Mode.
            """
            if buddy_owns_conversation(session):
                return False
            logger.info(
                "VoiceSession: ambient context injection suspended",
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "type": msg_type,
                    "owner": str(session.userdata.owner),
                },
            )
            return True

        def _handle_bridge(msg: dict, participant_identity: str, topic: str) -> None:
            del topic
            if bridge is None:
                return
            if participant_identity != user_id:
                logger.warn(
                    "bridge: control packet participant rejected",
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "participant": participant_identity,
                        "type": msg.get("type"),
                    },
                )
                return
            bridge.handle(msg)

        def _handle_output_mode(msg: dict, participant_identity: str, topic: str) -> None:
            context_tasks.append(
                asyncio.create_task(
                    output_mode_controller.apply_control(
                        msg, participant_identity, topic
                    ),
                    name=f"voice-output-mode-{session_id[:8]}",
                )
            )

        def _handle_artifact_ack(msg: dict, participant_identity: str, topic: str) -> None:
            artifact_delivery.handle_ack(msg, participant_identity, topic)

        def _handle_material_ack(msg: dict, participant_identity: str, topic: str) -> None:
            interview_materials.handle_overlay_ack(msg, participant_identity, topic)

        def _handle_guide_mode(msg: dict, participant_identity: str, topic: str) -> None:
            del topic
            guide.apply_control(msg, participant_identity)

        def _handle_guide_heartbeat(
            msg: dict, participant_identity: str, topic: str
        ) -> None:
            del topic
            guide.apply_heartbeat(msg, participant_identity)

        def _handle_screen_context(
            msg: dict, participant_identity: str, topic: str
        ) -> None:
            nonlocal screen_context_fired
            del participant_identity, topic
            if screen_context_fired or _ambient_context_suspended(SCREEN_CONTEXT_TYPE):
                return
            screen_context_fired = True
            context_tasks.append(
                asyncio.create_task(
                    deliver_screen_context(
                        session,
                        context_before=str(msg.get("context_before", "")),
                        field_type=msg.get("field_type"),
                        app=msg.get("app"),
                        session_id=session_id,
                        user_id=user_id,
                        on_instruction=turn_metrics.note_screen_text_context,
                    ),
                    name=f"voice-screen-ctx-{session_id[:8]}",
                )
            )

        def _handle_screen_context_unavailable(
            msg: dict, participant_identity: str, topic: str
        ) -> None:
            del topic
            if participant_identity != user_id:
                return
            screen_context.note_unavailable(str(msg.get("reason") or ""))

        def _handle_ocr_context(msg: dict, participant_identity: str, topic: str) -> None:
            del participant_identity, topic
            if _ambient_context_suspended(OCR_CONTEXT_TYPE):
                return
            context_tasks.append(
                asyncio.create_task(
                    deliver_screen_context(
                        session,
                        context_before=str(msg.get("text", "")),
                        field_type=None,
                        app=None,
                        session_id=session_id,
                        user_id=user_id,
                        on_instruction=turn_metrics.note_screen_text_context,
                    ),
                    name=f"voice-ocr-ctx-{session_id[:8]}",
                )
            )

        def _handle_text_input(msg: dict, participant_identity: str, topic: str) -> None:
            if participant_identity != user_id:
                logger.warn(
                    "VoiceSession: typed message packet rejected",
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "participant": participant_identity,
                        "topic": topic,
                    },
                )
                return
            if surface != "desktop" and not topic and not msg.get("client_message_id"):
                typed_messages.submit_legacy(text=str(msg.get("text", "")))
                return
            if topic != CLIENT_EVENTS_TOPIC:
                logger.warn(
                    "VoiceSession: typed message packet rejected",
                    {
                        "session_id": session_id,
                        "user_id": user_id,
                        "participant": participant_identity,
                        "topic": topic,
                    },
                )
                return
            typed_messages.submit(
                text=str(msg.get("text", "")),
                client_message_id=str(msg.get("client_message_id", "")),
                generation=msg.get("generation"),
            )

        # One named handler per client message type, instead of an eight-branch
        # if/elif every new control message had to be threaded into. Dispatch is a
        # lookup, so no ordering between types is implied or relied on; the only
        # ordering that matters is the pre-start buffering below.
        control_handlers: dict[str, Callable[[dict, str, str], None]] = {
            OUTPUT_MODE_TYPE: _handle_output_mode,
            ARTIFACT_DISPLAYED_TYPE: _handle_artifact_ack,
            MATERIAL_OVERLAY_SHOWN_TYPE: _handle_material_ack,
            GUIDE_MODE_TYPE: _handle_guide_mode,
            GUIDE_HEARTBEAT_TYPE: _handle_guide_heartbeat,
            SCREEN_CONTEXT_TYPE: _handle_screen_context,
            SCREEN_CONTEXT_UNAVAILABLE_TYPE: _handle_screen_context_unavailable,
            OCR_CONTEXT_TYPE: _handle_ocr_context,
            TEXT_INPUT_TYPE: _handle_text_input,
            **{control: _handle_bridge for control in BRIDGE_CONTROL_TYPES},
        }

        def _dispatch_context_payload(msg: dict, participant_identity: str, topic: str) -> None:
            handler = control_handlers.get(str(msg.get("type") or ""))
            if handler is not None:
                handler(msg, participant_identity, topic)

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
            participant = getattr(packet, "participant", None)
            participant_identity = str(getattr(participant, "identity", "") or "")
            topic = str(getattr(packet, "topic", "") or "")
            if session_live:
                _dispatch_context_payload(msg, participant_identity, topic)
            else:
                pending_context_payloads.append((msg, participant_identity, topic))

        ctx.room.on("data_received", _on_data_received)

        arize_context_token = bind_arize_context(
            session_id=session_id,
            surface=surface,
        )

        async def _flush_arize_spans() -> None:
            await asyncio.to_thread(flush_arize_tracing)

        ctx.add_shutdown_callback(_flush_arize_spans)

        try:
            await session.start(
                room=ctx.room,
                agent=buddy,
                room_options=room_io.RoomOptions(
                    participant_identity=user_id,
                    audio_input=room_io.AudioInputOptions(
                        sample_rate=16000,
                        frame_size_ms=20,
                        # Inbound voice isolation. Passed as a SELECTOR so the model is
                        # chosen per participant (agents skipped) rather than applied to
                        # everyone; see build_noise_cancellation() for why that matters
                        # both for quality and for the per-minute bill.
                        noise_cancellation=build_noise_cancellation,
                    ),
                    audio_output=room_io.AudioOutputOptions(
                        sample_rate=24000,  # Cartesia output rate
                    ),
                ),
            )

            # The session is live: process any context packet that arrived during startup,
            # then let the handler dispatch live ones directly.
            session_live = True
            # Armed from here, not earlier: before start there is no pipeline to hear
            # anything with, so a grace period measured from before this point would
            # accuse a session that was merely still building.
            liveness_task = asyncio.create_task(
                watch_input_liveness(
                    session=session,
                    ctx=ctx,
                    liveness=liveness,
                    session_id=session_id,
                    user_id=user_id,
                ),
                name=f"voice-input-liveness-{session_id[:8]}",
            )
            guide.start()
            # Before any buffered packet can produce speech: detach the audio
            # sink when the token asked for text output, and acknowledge the
            # resolved mode either way so the desktop knows this worker
            # understands output modes at all.
            await output_mode_controller.apply_initial()
            # Announce HOLD before flushing buffered packets so hold_ready reaches the
            # desktop ahead of any handover reply and no early control packet is lost.
            if bridge is not None:
                await bridge.start()
            for _payload, _participant_identity, _topic in pending_context_payloads:
                _dispatch_context_payload(_payload, _participant_identity, _topic)
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
            await recorder.flush_action_receipts()

            if bridge is not None:
                await bridge.aclose()

            session_end_iso = datetime.now(UTC).isoformat()
            elapsed_ms = int((time.monotonic() - session_start_mono) * 1000)

            # Free tier only: bank this call's seconds against today's voice budget so the
            # per-day total carries across calls. Fire-and-forget; never blocks teardown.
            if session_context.user_tier == "free":
                asyncio.create_task(
                    add_free_voice_seconds(user_id, elapsed_ms // 1000),
                    name=f"voice-budget-write-{session_id[:8]}",
                )

            try:
                await run_post_session_pipeline(
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
                    # What the inbound path actually did, so a zero-turn session can
                    # say WHY it was silent instead of only that it was.
                    liveness_verdict=liveness.verdict,
                    participant_linked=liveness.participant_linked,
                    audio_track_seen=liveness.audio_track_seen,
                    closed_by_idle_timeout=recorder.closed_by_idle_timeout,
                    llm_usage=recorder.llm_usage_totals,
                    llm_fallback_events=recorder.llm_fallback_events,
                )
            except Exception as exc:
                logger.error(
                    "VoiceSession: durable post-session finalization failed",
                    {
                        "user_id": user_id,
                        "session_id": session_id,
                        "error_type": type(exc).__name__,
                    },
                )
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
            await guide.close()
            # Stops the research narration poller; the run itself is durable
            # on the backend and the desktop outbox picks up its outcome.
            await buddy.close_research_narrator()
            await typed_messages.close()
            if voice_limit_task is not None:
                voice_limit_task.cancel()
            if liveness_task is not None:
                liveness_task.cancel()
            for task in context_tasks:
                task.cancel()
            reset_arize_context(arize_context_token)


if __name__ == "__main__":
    logger.info(
        "VoiceWorker: starting",
        {
            "pid": os.getpid(),
            "livekit_url": settings.LIVEKIT_URL,
            "livekit_configured": settings.livekit_configured,
            "deepgram_configured": bool(settings.DEEPGRAM_API_KEY),
            "cartesia_configured": bool(settings.CARTESIA_API_KEY),
            "anthropic_configured": bool(settings.ANTHROPIC_API_KEY),
            "firebase_web_api_key_configured": bool(settings.FIREBASE_WEB_API_KEY),
            "backend_internal_url": settings.BACKEND_INTERNAL_URL,
            **worker_revision_fields(),
        },
    )
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
