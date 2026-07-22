"""Memory-seeded session opener.

The static ``CASUAL_GREETINGS`` list in ``buddy_agent.py`` guarantees a sub-1s
hello, but every session opens the same way. This module races a cheap LLM call
(seeded with the session digest) against that fallback: ``voice_agent`` starts
the task right after ``gather_session_context``, and ``BuddyAgent.on_enter``
waits at most ``settings.VOICE_GREETING_SEED_BUDGET_S`` for it before falling
back to a static line. Fail-open everywhere: any error or timeout returns ""
and the static greeting speaks instead.
"""

from __future__ import annotations

import asyncio

from ...lib.logger import logger
from ...services.model_provider import get_model_provider
from .context import SessionContext

# The opener teaches a category (a friend's hello that lands personal), never a
# fixed line. The model decides whether the digest holds anything worth a
# callback; an empty/none answer is a valid outcome and falls back to static.
_OPENER_SYSTEM_PROMPT = (
    "You write the very first spoken line of a voice call from Buddy, the user's "
    "closest friend. One short, casual hello (under 15 words), warm and easygoing, "
    "the way a friend who remembers them opens a call. If the context below holds "
    "ONE thing genuinely worth a light callback (something they were doing, chasing, "
    "or feeling last time), weave it in naturally as a greeting, not a question "
    "stack and never a recap. If nothing is clearly worth referencing, or the "
    "context is empty, respond with exactly NONE. Never invent details, never "
    "mention notes or memory, no emojis, no quotes around the line. Lowercase, "
    "contracted, natural for text-to-speech."
)


def start_opener_task(
    session_context: SessionContext, *, session_id: str, user_id: str
) -> "asyncio.Task[str]":
    """Kick off the seeded-opener LLM call; resolves to "" on any failure."""

    async def _generate() -> str:
        try:
            digest_parts = [
                part
                for part in (
                    session_context.last_session_summary
                    and f"Last conversation: {session_context.last_session_summary}",
                    session_context.memory_summary
                    and f"What Buddy remembers: {session_context.memory_summary}",
                    session_context.aura_summary
                    and f"Who they are: {session_context.aura_summary}",
                )
                if part
            ]
            if not digest_parts:
                return ""
            opener = await get_model_provider().cheap(
                "\n".join(digest_parts),
                system=_OPENER_SYSTEM_PROMPT,
            )
            line = str(opener or "").strip().strip('"')
            if not line or line.upper() == "NONE" or len(line) > 120:
                return ""
            return line
        except Exception as exc:
            logger.warn("VoiceSession: seeded opener failed open", {
                "session_id": session_id,
                "user_id": user_id,
                "error": str(exc),
            })
            return ""

    return asyncio.create_task(_generate(), name=f"voice-opener-{session_id[:8]}")


async def resolve_opener(task: "asyncio.Task[str] | None", budget_s: float) -> str:
    """Wait briefly for the seeded opener; "" means use the static fallback."""
    if task is None:
        return ""
    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=budget_s)
    except (TimeoutError, asyncio.CancelledError):
        return ""
    except Exception:
        return ""
