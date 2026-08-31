"""Pre-session context assembly.

Fans out every Firestore read plus the tier lookup in parallel under one hard
timeout, then collapses the results into a single typed `SessionContext`. Each
source has exactly one declared default (in `_CONTEXT_SOURCES`), so a timeout or
a per-fetch failure degrades to that default with no second source of truth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from dataclasses import dataclass, field
from typing import Any

from ...lib.logger import logger
from ...services.entitlement import (
    get_remaining_free_voice_seconds,
    get_user_effective_tier,
)
from .fetchers import (
    fetch_archive_context,
    fetch_connector_states,
    fetch_graph_digest,
    fetch_last_session_summary,
    fetch_memory_summary,
    fetch_text_handoff,
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
    connector_states: dict[str, bool] = field(default_factory=dict)
    text_chat_context: str = ""

    @property
    def prompt_context_vars(self) -> dict[str, str]:
        """The values rendered once in the voice prompt's final session block."""
        timezone = self.profile["timezone"]
        return {
            "name": self.profile["name"],
            "timezone": timezone,
            "local_time": local_time_in_zone(timezone),
            "local_date": local_date_in_zone(timezone),
            # Pre-wrapped exactly like graph_context: "" when there is nothing to carry,
            # so a session with no text handoff renders byte-identically to before this
            # slot existed and the prompt cache boundary is unaffected.
            "text_chat_context": (
                "\n            Just before this call they were typing to you:\n            "
                + self.text_chat_context.replace("\n", "\n            ")
                if self.text_chat_context
                else ""
            ),
            "memory_summary": self.memory_summary or "(nothing yet — first conversation)",
            "graph_context": self.graph_context,
            "last_session_context": self.last_session_summary,
            "archive_context": self.archive_context,
            "user_aura_profile": self.aura_summary,
        }


async def gather_session_context(
    user_id: str, session_id: str, conversation_id: str = ""
) -> SessionContext:
    """Fetch profile, memory, last session, archive, aura, and tier in parallel.

    The hard 1.5s ceiling enforces the under-1s greeting feel. On timeout every
    source falls back to its declared default; on a partial failure only the
    failed source does (and is logged with its name).
    """
    # One record per source. The name is what a failure is logged against and
    # what the result is read back by, so a source cannot drift out of step with
    # its own default the way four hand-maintained parallel lists could.
    sources: list[tuple[str, Awaitable[Any], Any]] = [
        (
            "user_profile",
            fetch_user_profile(user_id),
            {"name": "there", "timezone": "UTC", "voice_id": ""},
        ),
        ("memory_summary", fetch_memory_summary(user_id), ""),
        (
            "last_session_summary",
            fetch_last_session_summary(user_id),
            {"summary": "", "last_session_at": ""},
        ),
        ("archive_context", fetch_archive_context(user_id), {"archive_summary": ""}),
        (
            "user_aura_profile",
            fetch_user_aura_profile(user_id),
            {"summary": "", "dominant_tone": "", "dominant_emotion": ""},
        ),
        ("user_tier", get_user_effective_tier(user_id), "unknown"),
        (
            "remaining_free_voice_seconds",
            get_remaining_free_voice_seconds(user_id),
            None,
        ),
        ("graph_context", fetch_graph_digest(user_id), ""),
        ("connector_states", fetch_connector_states(user_id), {}),
    ]
    # Cross-lane continuity. Only desktop sends a conversation_id today, and only after
    # it has handed its recent text turns to /chat/handoff, so this read is skipped
    # entirely for every other caller. It rides the same 1.5s ceiling and the same
    # per-source default as everything above: a slow or missing handoff means the call
    # starts with no text context, never that the greeting waits for one.
    if conversation_id:
        sources.append(
            ("text_chat_context", fetch_text_handoff(user_id, conversation_id), "")
        )

    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(
                *(coroutine for _, coroutine, _ in sources), return_exceptions=True
            ),
            timeout=PRE_SESSION_FETCH_TIMEOUT_S,
        )
    except TimeoutError:
        logger.warn("VoiceSession: pre-session fetch timed out, using defaults", {
            "session_id": session_id, "user_id": user_id,
        })
        raw_results = [default for _, _, default in sources]

    resolved: dict[str, Any] = {}
    for (name, _coroutine, default), value in zip(sources, raw_results):
        if isinstance(value, BaseException):
            logger.warn("VoiceSession: pre-session fetch failed", {
                "session_id": session_id, "user_id": user_id,
                "source": name, "error": str(value),
            })
            resolved[name] = default
        else:
            resolved[name] = value

    profile = resolved["user_profile"]
    last_session = resolved["last_session_summary"]
    archive_data = resolved["archive_context"]
    aura_profile = resolved["user_aura_profile"]
    graph_digest = resolved["graph_context"]

    return SessionContext(
        profile=profile,
        memory_summary=resolved["memory_summary"],
        last_session_summary=last_session.get("summary", ""),
        last_session_at=last_session.get("last_session_at", ""),
        archive_context=archive_data.get("archive_summary", ""),
        aura_summary=aura_profile.get("summary", ""),
        dominant_tone=aura_profile.get("dominant_tone", ""),
        dominant_emotion=aura_profile.get("dominant_emotion", ""),
        user_tier=resolved["user_tier"],
        remaining_free_voice_seconds=resolved["remaining_free_voice_seconds"],
        graph_context=(
            "\n\n            Related long-term memory:\n            "
            + graph_digest.replace("\n", "\n            ")
            if graph_digest
            else ""
        ),
        connector_states=resolved["connector_states"],
        text_chat_context=resolved.get("text_chat_context", ""),
    )
