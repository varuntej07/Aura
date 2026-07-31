"""Generate Buddy's short reply to a shade answer.

When the user answers a curiosity follow-up inside the notification (without
opening the app), Buddy still has to say something back — a friend does not go
silent after you tell them something. This is one cheap LLM call that continues
the thread warmly in one or two sentences.

Never raises: any failure falls back to a brief, genuine acknowledgement so the
reply endpoint always has something to show in the notification shade.
"""

from __future__ import annotations

from ...lib.logger import logger
from ...prompts import THREAD_RESPONDER_SYSTEM_PROMPT, thread_responder_user_prompt
from ..model_provider import ModelProvider
from .models import Thread

# Buddy replies in the notification shade, so keep it tight — a long reply gets
# truncated by the OS anyway.
RESPONSE_MAX_CHARS = 220


def _fallback(reply: str) -> str:
    cleaned = (reply or "").strip()
    if cleaned:
        return "love that. tell me more whenever you want."
    return "gotcha. i'm around whenever you wanna get into it."


async def generate_thread_reply(
    models: ModelProvider,
    thread: Thread,
    *,
    question: str,
    user_reply: str,
) -> str:
    """One LLM call continuing the thread. Returns a safe fallback on failure."""
    prompt = thread_responder_user_prompt(
        question=question,
        original_mention=thread.trigger_text,
        user_reply=user_reply,
    )
    try:
        result = await models.cheap(prompt, system=THREAD_RESPONDER_SYSTEM_PROMPT, temperature=0.7)
        text = (result if isinstance(result, str) else str(result)).strip()
        return (text or _fallback(user_reply))[:RESPONSE_MAX_CHARS]
    except Exception as exc:
        logger.warn("threads.thread_responder: reply generation failed, using fallback", {
            "thread_id": thread.thread_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return _fallback(user_reply)
