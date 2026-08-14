"""One Gemini Flash call that turns an open-loop thread into a curious question.

This is the tone-critical piece. Buddy is a close friend who just remembered
something the user mentioned and is genuinely intrigued — never a coach, never
an auditor. The framer NEVER asks whether a task was completed; it asks what the
thing is, who it's for, how the user feels about it. The goal of every push is
to earn one more true fact about the user.

Selection (which thread, whether to send) already happened in the reflector with
pure Python. The framer's only job is the words. It never raises: any failure
falls back to a safe, generic-but-warm question so the reflector always gets a
valid result.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ...prompts import THREAD_FRAMER_SYSTEM_PROMPT, thread_framer_user_prompt
from ..model_provider import ModelProvider
from ..signal_engine.notification_framer import (
    strip_long_dashes,
    truncate_at_word_boundary,
)
from .models import Thread, ThreadSource

# Hard caps enforced after the model returns. The prompt asks for the same
# numbers but the LLM occasionally overshoots; truncation keeps the FCM payload
# and the on-notification chips inside platform limits.
FOLLOW_UP_TITLE_MAX_CHARS = 40
FOLLOW_UP_BODY_MAX_CHARS = 90
SUGGESTED_REPLY_MAX_CHARS = 24
MAX_SUGGESTED_REPLIES = 3
MIN_SUGGESTED_REPLIES = 2


class FramedFollowUp(BaseModel):
    """The words Buddy sends for a curiosity follow-up."""

    title: str = Field(..., description="Push title, <= 40 chars, casual.")
    body: str = Field(..., description="The question, <= 90 chars, like a text from a friend.")
    suggested_replies: list[str] = Field(
        ...,
        description="2-3 effortless conversation-openers, each <= 24 chars.",
    )


class FollowUpFramingContext(BaseModel):
    """Compact read-only view of the user the framer is allowed to see."""

    dominant_tone: str | None = None
    depth_level: int = 1                 # emotional_engagement_level, 1..5
    top_interests: list[str] = Field(default_factory=list)
    time_band: str = "anytime"           # morning | midday | afternoon | evening | late


# Few-shot examples steer the tone hard away from "teacher" and toward "friend".
def _build_prompt(thread: Thread, ctx: FollowUpFramingContext) -> str:
    base = thread_framer_user_prompt(
        tone=ctx.dominant_tone or "neutral",
        depth_level=ctx.depth_level,
        top_interests=ctx.top_interests,
        time_band=ctx.time_band,
        trigger_text=thread.trigger_text,
        source=str(thread.source),
        known_summary=thread.known_summary,
        unknown=thread.unknown,
    )
    return (
        base
        + "\nSafety for suggested replies: use neutral conversation openings only. "
        "Never invent a cause, diagnosis, motive, identity, relationship, or explanation; "
        "never trivialize the subject; never write a reply that could out private information."
    )


def _safe_fallback(thread: Thread) -> FramedFollowUp:
    """Generic-but-warm question used when the LLM call fails."""
    snippet = (thread.trigger_text or "").strip()
    if thread.source == ThreadSource.AURA_GAP:
        body = "mind if I ask you something? trying to know you better"
        replies = ["sure", "go for it", "maybe later"]
    elif snippet:
        short = snippet if len(snippet) <= 40 else snippet[:39].rstrip() + "…"
        body = f"what's the story with {short}?"
        replies = ["tell you about it", "it's a long one", "later"]
    else:
        body = "what have you been up to lately?"
        replies = ["a lot honestly", "not much", "i'll tell you"]
    return FramedFollowUp(
        title="hey",
        body=body[:FOLLOW_UP_BODY_MAX_CHARS],
        suggested_replies=[r[:SUGGESTED_REPLY_MAX_CHARS] for r in replies],
    )


def _normalise(framed: FramedFollowUp, thread: Thread) -> FramedFollowUp:
    """Enforce char caps and the 2-3 reply count after the model returns."""
    replies = [
        strip_long_dashes(r.strip())[:SUGGESTED_REPLY_MAX_CHARS]
        for r in framed.suggested_replies
        if r and r.strip()
    ][:MAX_SUGGESTED_REPLIES]
    # A model that returns 0 or 1 usable replies still must satisfy the UI's
    # minimum, so fall back rather than ship a single lonely chip.
    if len(replies) < MIN_SUGGESTED_REPLIES:
        replies = _safe_fallback(thread).suggested_replies
    return FramedFollowUp(
        title=truncate_at_word_boundary(
            strip_long_dashes(framed.title.strip()), FOLLOW_UP_TITLE_MAX_CHARS
        ) or "hey",
        body=truncate_at_word_boundary(
            strip_long_dashes(framed.body.strip()), FOLLOW_UP_BODY_MAX_CHARS
        ),
        suggested_replies=replies,
    )


async def frame_follow_up(
    models: ModelProvider,
    thread: Thread,
    ctx: FollowUpFramingContext,
) -> FramedFollowUp:
    """One LLM call. Returns a safe fallback on any failure."""
    prompt = _build_prompt(thread, ctx)
    try:
        result = await models.cheap(
            prompt,
            system=THREAD_FRAMER_SYSTEM_PROMPT,
            response_model=FramedFollowUp,
            temperature=0.7,
        )
        return _normalise(cast(FramedFollowUp, result), thread)
    except Exception as exc:
        logger.warn("threads.thread_framer: LLM framing failed, using fallback", {
            "thread_id": thread.thread_id,
            "source": str(thread.source),
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return _safe_fallback(thread)
