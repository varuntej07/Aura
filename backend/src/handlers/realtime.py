"""realtime.py — mint a short-lived OpenAI Realtime ephemeral secret for the desktop.

The desktop overlay opens an OpenAI Realtime speech-to-speech leg (client-direct over
WebRTC) the instant the user double-taps, so the user hears a voice in under a second
while the LiveKit cascade worker is still cold-starting on LiveKit Cloud. See
Aura-Desktop's bridge coordinator and the plan for the full handover protocol.

Security: the real OPENAI_API_KEY never leaves the server. This endpoint verifies the
Firebase caller, then asks OpenAI for a per-session ephemeral secret (`ek_...`, ~60s TTL)
that the desktop uses to authenticate its WebRTC session. Response is `no-store`.

Latency: this endpoint is on the tap-to-first-voice critical path, so it does the
minimum. It does NOT run gather_session_context or any per-user aggregation; the Realtime
leg is deliberately gather-only and short-lived, and the full personalized cascade takes
over at handover. Keep it that way.
"""

from __future__ import annotations

import asyncio
from typing import Any

from fastapi import Request
from fastapi.responses import JSONResponse

from ..config.settings import settings
from ..lib.logger import logger
from ..prompts import DESKTOP_REALTIME_GATHER_INSTRUCTIONS
from ..services.request_auth import decode_firebase_claims

# The mint sits on the tap-to-first-voice path, so cap each attempt and retry once
# on a transient connection blip before dropping the desktop to the cold path.
_MINT_TIMEOUT_S = 4.0
_MINT_ATTEMPTS = 2

_client: Any = None


def _get_openai_client() -> Any:
    """Lazy AsyncOpenAI singleton, mirroring openai_chat_fallback._get_openai_client."""
    global _client
    if _client is None:
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not set — realtime bridge unavailable")
        from openai import AsyncOpenAI  # type: ignore

        # .strip() is mandatory here: the mounted secret carries a trailing newline,
        # and an Authorization header value with a CR/LF is rejected by httpx before
        # the request is even sent (LocalProtocolError, surfaced as APIConnectionError).
        # Every other key in this codebase is stripped the same way (see voice/pipelines.py).
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY.strip())
    return _client


async def create_realtime_session(request: Request) -> JSONResponse:
    """POST /realtime/session — return an ephemeral OpenAI Realtime secret.

    401 if unauthenticated, 503 when the bridge is disabled or misconfigured (the desktop
    then falls back to the plain cold LiveKit path). The response carries the ephemeral
    secret value and its expiry plus the model/voice the desktop should negotiate with.
    """
    claims = decode_firebase_claims(request.headers)
    if not claims:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)
    user_id: str = claims.get("uid") or claims.get("sub") or ""
    if not user_id:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401)

    if not settings.REALTIME_BRIDGE_ENABLED:
        return JSONResponse({"detail": "realtime bridge disabled"}, status_code=503)

    session_config = {
        "type": "realtime",
        "model": settings.OPENAI_REALTIME_MODEL,
        "instructions": DESKTOP_REALTIME_GATHER_INSTRUCTIONS,
        "output_modalities": ["audio"],
        "audio": {
            "input": {
                # Transcription ON: the desktop needs committed user-turn text to
                # build the ordered handover transcript it seeds into LiveKit.
                "transcription": {"model": settings.OPENAI_REALTIME_TRANSCRIBE_MODEL},
                "turn_detection": {"type": "server_vad"},
            },
            "output": {"voice": settings.OPENAI_REALTIME_VOICE},
        },
    }
    expires_after = {
        "anchor": "created_at",
        "seconds": settings.OPENAI_REALTIME_SECRET_TTL_S,
    }

    secret = None
    last_exc: Exception | None = None
    for attempt in range(_MINT_ATTEMPTS):
        try:
            client = _get_openai_client()
            secret = await asyncio.wait_for(
                client.realtime.client_secrets.create(
                    expires_after=expires_after,
                    session=session_config,
                ),
                timeout=_MINT_TIMEOUT_S,
            )
            break
        except Exception as exc:
            # Transient connection blips (APIConnectionError, timeout) get one retry;
            # a config/key problem fails both attempts and drops to the cold path.
            last_exc = exc
            if attempt + 1 < _MINT_ATTEMPTS:
                await asyncio.sleep(0.25)

    if secret is None:
        # Never leak provider exception text or the real key. The desktop treats any
        # non-200 as "no bridge" and falls back to the cold LiveKit path. A persistent
        # failure here is environmental: Cloud Run egress to api.openai.com, a missing
        # OPENAI_API_KEY, or an openai SDK too old for realtime.client_secrets.
        # Log only the cause TYPE, never its message: for a LocalProtocolError the
        # message echoes the offending Authorization header, which contains the raw
        # API key. The type alone (ConnectError / ConnectTimeout / LocalProtocolError
        # / ...) is enough to tell the network/config layer apart without leaking a
        # secret into the logs.
        cause = getattr(last_exc, "__cause__", None)
        logger.warn(
            "realtime_session: ephemeral secret mint failed",
            {
                "user_id": user_id,
                "error_type": type(last_exc).__name__ if last_exc else "None",
                "cause_type": type(cause).__name__ if cause is not None else None,
                "attempts": _MINT_ATTEMPTS,
            },
        )
        return JSONResponse({"detail": "realtime unavailable"}, status_code=503)

    response = JSONResponse(
        {
            "client_secret": secret.value,
            "expires_at": secret.expires_at,
            "model": settings.OPENAI_REALTIME_MODEL,
            "voice": settings.OPENAI_REALTIME_VOICE,
        }
    )
    response.headers["Cache-Control"] = "no-store"
    return response
