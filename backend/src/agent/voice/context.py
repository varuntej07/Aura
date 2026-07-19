"""Pre-session context assembly.

Fans out every Firestore read plus the tier lookup in parallel under one hard
timeout, then collapses the results into a single typed `SessionContext`. Each
source has exactly one declared default (in `_CONTEXT_SOURCES`), so a timeout or
a per-fetch failure degrades to that default with no second source of truth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger
from ...services.entitlement import (
    get_remaining_free_voice_seconds,
    get_user_effective_tier,
)
from .fetchers import (
    fetch_archive_context,
    fetch_graph_digest,
    fetch_last_session_summary,
    fetch_memory_summary,
    fetch_user_aura_profile,
    fetch_user_profile,
)
from .prompt_context import local_date_in_zone, local_time_in_zone

# Hard cap on the parallel profile + memory fetch before session.start.
# A LiveKit session can't speak its greeting until on_enter resolves, and
# the agent feels conversational only if the first audio lands inside ~1s.
# 1.5s is the budget that still leaves margin for STT/LLM/TTS warm-up.
PRE_SESSION_FETCH_TIMEOUT_S = 1.5


@dataclass
class SessionContext:
    """Everything pulled before session start, fully defaulted."""

    profile: dict
    memory_summary: str
    last_session_summary: str
    last_session_at: str
    archive_context: str
    aura_summary: str
    dominant_tone: str
    dominant_emotion: str
    user_tier: str
    remaining_free_voice_seconds: int | None
    graph_context: str = ""

    @property
    def prompt_context_vars(self) -> dict[str, str]:
        """The substitution map the voice system prompt is formatted with."""
        timezone = self.profile["timezone"]
        return {
            "name": self.profile["name"],
            "timezone": timezone,
            "local_time": local_time_in_zone(timezone),
            "local_date": local_date_in_zone(timezone),
            "memory_summary": self.memory_summary or "(nothing yet — first conversation)",
            "graph_context": self.graph_context,
            "last_session_context": self.last_session_summary,
            "last_session_at": self.last_session_at,
            "archive_context": self.archive_context,
            "user_aura_profile": self.aura_summary,
        }


async def gather_session_context(user_id: str, session_id: str) -> SessionContext:
    """Fetch profile, memory, last session, archive, aura, and tier in parallel.

    The hard 1.5s ceiling enforces the under-1s greeting feel. On timeout every
    source falls back to its declared default; on a partial failure only the
    failed source does (and is logged with its name).
    """
    # (coroutine, default) pairs — the single source of truth for each fetch's
    # fallback value, used identically on timeout and on per-fetch failure.
    sources: list[tuple[Awaitable[Any], Any]] = [
        (fetch_user_profile(user_id), {"name": "there", "timezone": "UTC"}),
        (fetch_memory_summary(user_id), ""),
        (fetch_last_session_summary(user_id), {"summary": "", "last_session_at": ""}),
        (fetch_archive_context(user_id), {"archive_summary": ""}),
        (
            fetch_user_aura_profile(user_id),
            {"summary": "", "dominant_tone": "", "dominant_emotion": ""},
        ),
        (get_user_effective_tier(user_id), "unknown"),
        (get_remaining_free_voice_seconds(user_id), None),
    ]
    coroutines = [coro for coro, _ in sources]
    defaults = [default for _, default in sources]
    names = [
        "user_profile", "memory_summary", "last_session_summary",
        "archive_context", "user_aura_profile", "user_tier",
        "remaining_free_voice_seconds",
    ]
    if settings.GRAPH_READ_VOICE:
        graph_source = (fetch_graph_digest(user_id), "")
        sources.append(graph_source)
        coroutines.append(graph_source[0])
        defaults.append(graph_source[1])
        names.append("graph_context")

    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(*coroutines, return_exceptions=True),
            timeout=PRE_SESSION_FETCH_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warn("VoiceSession: pre-session fetch timed out, using defaults", {
            "session_id": session_id, "user_id": user_id,
        })
        raw_results = list(defaults)

    resolved: list = []
    for name, value, default in zip(names, raw_results, defaults):
        if isinstance(value, BaseException):
            logger.warn("VoiceSession: pre-session fetch failed", {
                "session_id": session_id, "user_id": user_id,
                "source": name, "error": str(value),
            })
            resolved.append(default)
        else:
            resolved.append(value)

    profile, memory_summary, last_session, archive_data, aura_profile = resolved[:5]
    user_tier, remaining_free_voice_seconds = resolved[5:7]
    graph_digest = resolved[7] if settings.GRAPH_READ_VOICE else ""

    return SessionContext(
        profile=profile,
        memory_summary=memory_summary,
        last_session_summary=last_session.get("summary", ""),
        last_session_at=last_session.get("last_session_at", ""),
        archive_context=archive_data.get("archive_summary", ""),
        aura_summary=aura_profile.get("summary", ""),
        dominant_tone=aura_profile.get("dominant_tone", ""),
        dominant_emotion=aura_profile.get("dominant_emotion", ""),
        user_tier=user_tier,
        remaining_free_voice_seconds=remaining_free_voice_seconds,
        graph_context=(
            "\n\n            Related long-term memory:\n            "
            + graph_digest.replace("\n", "\n            ")
            if graph_digest
            else ""
        ),
    )
