"""Pipeline error classification and client-facing error delivery.

Splits provider-exhausted/quota failures out of the generic bucket and pushes a
friendly `session.error` down the LiveKit data channel so the Flutter client
reacts immediately instead of waiting on its own silence watchdog.
"""

from __future__ import annotations

import json

from livekit.agents import JobContext

from ...lib.logger import logger


def classify_pipeline_error(error_text: str) -> tuple[str, str]:
    """Map a runtime pipeline error to (client_code, friendly_message).

    Pulls the 'we're out of API credit / the provider rejected our key' case out
    of the generic bucket so the app can honestly say it's an "our end" problem.
    This is the exact shape of the zero-credit hang: every LLM/TTS fallback fails
    with an auth/quota error and the user otherwise gets nothing.
    """
    lowered = error_text.lower()
    if any(
        marker in lowered
        for marker in (
            "insufficient", "quota", "credit", "billing", "payment",
            "401", "403", "unauthorized", "authentication", "rate limit",
        )
    ):
        return (
            "provider_unavailable",
            "Buddy's voice is having a moment on our end. Hang tight and try again shortly.",
        )
    if "tts" in lowered or "cartesia" in lowered or "audio_output" in lowered:
        return ("tts_pipeline_failed", "Buddy hit a snag mid-call. Mind tapping to start over?")
    return ("session_runtime_failed", "Buddy hit a snag mid-call. Mind tapping to start over?")


async def publish_client_error(ctx: JobContext, code: str, message: str) -> None:
    """Push a session.error down the LiveKit data channel so the Flutter client
    shows a friendly message immediately instead of waiting on its own watchdog.

    The payload shape matches VoiceServerEvent.fromJson on the client:
    {type: 'session.error', message, payload: {code}}.
    """
    try:
        payload = json.dumps({
            "type": "session.error",
            "message": message,
            "payload": {"code": code},
        }).encode("utf-8")
        await ctx.room.local_participant.publish_data(payload, reliable=True)
    except Exception as exc:
        logger.warn("VoiceSession: failed to publish client error", {
            "code": code, "error": str(exc),
        })
