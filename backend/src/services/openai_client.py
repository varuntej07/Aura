"""Shared lazy AsyncOpenAI singleton.

Promoted from handlers/realtime.py so cross-module consumers (the Realtime
secret mint, Interview Companion's transcription-secret mint, the meetings
Whisper fallback) stop importing a handler's private helper or constructing
their own client per call. ``model_provider`` and the chat fallbacks keep
their own clients on purpose - they carry provider-specific timeout policy.
"""

from __future__ import annotations

from typing import Any

from ..config.settings import settings

_client: Any = None


def get_async_openai() -> Any:
    """Lazy AsyncOpenAI singleton. Raises ValueError when OPENAI_API_KEY is
    unset so a misconfiguration is loud at the call site, not at request time
    inside the SDK."""
    global _client
    if _client is None:
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is not set")
        from openai import AsyncOpenAI  # type: ignore

        # .strip() is mandatory here: the mounted secret carries a trailing
        # newline, and an Authorization header value with a CR/LF is rejected
        # by httpx before the request is even sent (LocalProtocolError,
        # surfaced as APIConnectionError).
        _client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY.strip())
    return _client
