"""Short-lived Deepgram transcription credentials - the one mint owner.

``POST https://api.deepgram.com/v1/auth/grant`` exchanges the permanent API
key (presented as ``Token``) for a transcription-scoped JWT the client
presents as ``Bearer``. Per the official docs (developers.deepgram.com,
Token-Based Authentication): the TTL defaults to 30 seconds and caps at 3600,
the JWT carries ``usage::write`` for the voice APIs only, and a 403 means the
key lacks Member-level access. The permanent key never leaves this process.

Privacy: only exception TYPE names are logged, never messages - an httpx
protocol error can echo the Authorization header, which carries the real key.
"""

from __future__ import annotations

from ...config.settings import settings
from ...lib.logger import logger

_GRANT_URL = "https://api.deepgram.com/v1/auth/grant"
# Callers refresh ahead of expiry, so this never blocks a keystroke; bounded
# anyway because a hung provider must not hold a Cloud Run worker.
_TIMEOUT_S = 6.0
# Documented hard ceiling of /v1/auth/grant.
_MAX_TTL_S = 3600


async def mint_grant(*, ttl_seconds: int, caller: str) -> tuple[str, int] | None:
    """Mint one (access_token, expires_in_seconds) pair, or None on any
    failure. ``caller`` labels the logs so each surface stays diagnosable."""
    if not settings.DEEPGRAM_DICTATION_API_KEY:
        return None

    import httpx

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
            grant = await client.post(
                _GRANT_URL,
                headers={
                    # A raw API key is presented as `Token`; the JWT this
                    # returns is presented to the WebSocket as `Bearer`.
                    # .strip() is mandatory: a mounted secret carries a
                    # trailing newline, and httpx rejects a header value
                    # containing CR/LF before the request is even sent.
                    "Authorization": f"Token {settings.DEEPGRAM_DICTATION_API_KEY.strip()}",
                    "Content-Type": "application/json",
                },
                json={"ttl_seconds": min(int(ttl_seconds), _MAX_TTL_S)},
            )
    except Exception as exc:
        logger.warn(
            f"{caller}: deepgram token mint failed",
            {"error_type": type(exc).__name__},
        )
        return None

    if grant.status_code != 200:
        # Never echo the provider's body: it can quote the request. The
        # status alone separates a bad key from an outage.
        logger.warn(
            f"{caller}: deepgram token mint rejected",
            {"status": grant.status_code},
        )
        return None

    try:
        payload = grant.json()
        access_token = payload["access_token"]
        expires_in = int(float(payload.get("expires_in") or ttl_seconds))
    except Exception as exc:
        logger.warn(
            f"{caller}: deepgram token response unusable",
            {"error_type": type(exc).__name__},
        )
        return None

    if not isinstance(access_token, str) or not access_token or expires_in <= 0:
        return None
    return access_token, expires_in
