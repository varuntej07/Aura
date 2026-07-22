"""Voice-session telemetry: failure logging, per-turn metrics, lifecycle span.

All Cloud Logging emission for a voice session funnels through here so the log
field shapes stay consistent across the worker.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from uuid import uuid4

from ...lib.logger import logger
from .revision import worker_revision_fields

# Region tag for per-turn voice telemetry. LiveKit Cloud picks the edge region
# per connection and does not expose it cheaply, so we tag the deployment region
# from the environment. Set LIVEKIT_REGION at deploy time; otherwise "unknown".
VOICE_TELEMETRY_REGION = os.environ.get("LIVEKIT_REGION", "unknown")


def log_voice_failure(
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


def log_turn_metrics(
    *,
    session_id: str,
    user_id: str,
    role: str,
    metrics: dict,
    tier: str,
) -> None:
    """Emit per-turn component latency from a ChatMessage.metrics report.

    LiveKit splits per-turn telemetry across two messages: user turns carry the
    endpointing decision and STT transcription delay; assistant turns carry LLM
    time-to-first-token, TTS time-to-first-byte, and the end-of-utterance ->
    first-audio (e2e) latency that owns the perceived response gap. All values
    are converted to milliseconds. model/provider come from the per-turn
    metadata LiveKit attaches; region/tier are session-level tags so a turn can
    be sliced by deployment region and subscription tier later.
    """
    def _to_ms(seconds: object) -> int | None:
        return int(seconds * 1000) if isinstance(seconds, (int, float)) else None

    payload: dict = {
        "session_id": session_id,
        "user_id": user_id,
        "role": role,
        "tier": tier,
        "region": VOICE_TELEMETRY_REGION,
    }

    if role == "user":
        payload["endpointing_ms"] = _to_ms(metrics.get("end_of_turn_delay"))
        payload["stt_final_ms"] = _to_ms(metrics.get("transcription_delay"))
        payload["turn_hook_ms"] = _to_ms(metrics.get("on_user_turn_completed_delay"))
        stt_meta = metrics.get("stt_metadata") or {}
        payload["stt_model"] = stt_meta.get("model_name")
        payload["stt_provider"] = stt_meta.get("model_provider")
    elif role == "assistant":
        payload["llm_ttft_ms"] = _to_ms(metrics.get("llm_node_ttft"))
        payload["tts_ttfb_ms"] = _to_ms(metrics.get("tts_node_ttfb"))
        payload["eou_to_first_audio_ms"] = _to_ms(metrics.get("e2e_latency"))
        payload["playback_ms"] = _to_ms(metrics.get("playback_latency"))
        llm_meta = metrics.get("llm_metadata") or {}
        tts_meta = metrics.get("tts_metadata") or {}
        payload["llm_model"] = llm_meta.get("model_name")
        payload["llm_provider"] = llm_meta.get("model_provider")
        payload["tts_model"] = tts_meta.get("model_name")
        payload["tts_provider"] = tts_meta.get("model_provider")
    else:
        return

    if role == "assistant" and all(
        payload.get(field) is None
        for field in ("llm_ttft_ms", "tts_ttfb_ms", "eou_to_first_audio_ms")
    ):
        logger.warn("VoiceSession: turn metrics present but all latency fields null", {
            "session_id": session_id,
            "user_id": user_id,
            "role": role,
            "available_metric_keys": sorted(metrics.keys()),
        })

    logger.info("VoiceSession: turn metrics", payload)


@asynccontextmanager
async def voice_session_logger(
    user_id: str,
    room_name: str,
    *,
    session_id: str | None = None,
) -> AsyncIterator[str]:
    """Open a logged span for one voice session, yielding a fresh session_id.

    Logs start, unhandled error (re-raised), and close-with-duration so every
    session has a matching started/closed pair in Cloud Logging.
    """
    session_id = session_id or str(uuid4())
    start = time.monotonic()
    logger.info("VoiceSession: started", {
        "session_id": session_id, "user_id": user_id, "room": room_name,
        **worker_revision_fields(),
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
