"""
One Gemini Flash call per (user, content) pair to produce notification copy.

The framer never decides whether to send. It only writes the title, body,
and opening_chat_message after scoring has picked the content and the user.

Input has two parts:
  - candidate: the chosen content_pool item (source title/body/url/category)
  - user_context: a small read-only summary derived from UserAura
                  (top interests, dominant tone) and current local time

Output is a Pydantic FramedNotification. If the LLM call fails or returns
malformed JSON, the framer falls back to a safe template that uses the raw
source title and a generic Buddy-voice opener. The scoring loop always
gets a valid result back; it never has to handle exceptions from here.
"""

from __future__ import annotations

from datetime import datetime
from typing import cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ..model_provider import ModelProvider
from .content_pool import ScoredCandidate

# Hard limits enforced after the model returns. 
# The prompt says the same numbers but the LLM occasionally overshoots; 
# truncation guarantees the FCM payload stays inside platform limits
NOTIFICATION_TITLE_MAX_CHARS = 50
NOTIFICATION_BODY_MAX_CHARS = 100
OPENING_CHAT_MESSAGE_MAX_CHARS = 280


class FramedNotification(BaseModel):
    title: str = Field(..., description="Push title, <= 50 chars.")
    body: str = Field(..., description="Push body, <= 100 chars.")
    opening_chat_message: str = Field(
        ...,
        description="One or two sentences Buddy opens with when the user taps."
    )


class UserFramingContext(BaseModel):
    """Compact read-only view the framer sees about the user."""

    top_interests: list[str] = Field(default_factory=list)
    dominant_tone: str | None = None
    user_local_time_band: str = "anytime"   # morning | midday | afternoon | evening | late
    depth_level: int = 1                    # PRODUCT_STRATEGY section 13: 1..5


_FRAMER_SYSTEM_PROMPT = """\
You are Buddy, writing a single push notification to one specific user.
Scoring already chose the content and the moment. Your only job is the words.

Rules, all hard:
- title: at most 50 characters, sentence case, no emojis, no exclamation marks.
- body: at most 100 characters, one short sentence, feels like a text from a friend.
- opening_chat_message: one or two sentences Buddy says when the user taps the
  notification and the chat opens. Reference the content concretely.
- Never use em-dashes, en-dashes, or double hyphens. Rewrite to flow without them.
- Never use the word "exciting", "amazing", "great news", or other generic filler.
- Match the user's dominant_tone when set.
- depth_level >= 3 means you can reference the user's interests by name.
- depth_level == 5 means you can use light first-person familiarity ("noticed this for you").
- Output ONLY valid JSON matching the schema. No markdown fences. No prose.

Schema:
{
  "title": "string",
  "body": "string",
  "opening_chat_message": "string"
}
"""


def _build_framer_prompt(candidate: ScoredCandidate, user_context: UserFramingContext) -> str:
    interests_line = (
        ", ".join(user_context.top_interests[:3])
        if user_context.top_interests else "no strong interests recorded yet"
    )
    tone_line = user_context.dominant_tone or "neutral"
    return f"""\
            USER CONTEXT
            top_interests: {interests_line}
            dominant_tone: {tone_line}
            local_time_band: {user_context.user_local_time_band}
            depth_level: {user_context.depth_level}

            CONTENT
            source: {candidate.source}
            category: {candidate.category}
            title: {candidate.title}
            body: {candidate.body[:400]}
            url: {candidate.url}

            Write the notification now. JSON only.
        """


def _safe_fallback(candidate: ScoredCandidate) -> FramedNotification:
    title = (candidate.title or candidate.source or "Something for you")[:NOTIFICATION_TITLE_MAX_CHARS]
    body = (
        f"From {candidate.source}. Worth a look."
        if candidate.source else "Tap to read."
    )[:NOTIFICATION_BODY_MAX_CHARS]
    opening = (
        f"Came across this and thought of you: {candidate.title}"
        if candidate.title else "Came across something I thought you might like."
    )[:OPENING_CHAT_MESSAGE_MAX_CHARS]
    return FramedNotification(title=title, body=body, opening_chat_message=opening)


def _truncate(framed: FramedNotification) -> FramedNotification:
    return FramedNotification(
        title=framed.title[:NOTIFICATION_TITLE_MAX_CHARS],
        body=framed.body[:NOTIFICATION_BODY_MAX_CHARS],
        opening_chat_message=framed.opening_chat_message[:OPENING_CHAT_MESSAGE_MAX_CHARS],
    )


async def frame_notification(
    models: ModelProvider,
    candidate: ScoredCandidate,
    user_context: UserFramingContext,
) -> FramedNotification:
    """One LLM call. Returns a safe fallback on any failure."""
    prompt = _build_framer_prompt(candidate, user_context)
    try:
        result = await models.cheap(
            prompt,
            system=_FRAMER_SYSTEM_PROMPT,
            response_model=FramedNotification,
            temperature=0.6,
        )
        framed = cast(FramedNotification, result)
        return _truncate(framed)
    except Exception as exc:
        logger.warn("notification_framer: LLM framing failed, using fallback", {
            "content_id": candidate.content_id,
            "source": candidate.source,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return _safe_fallback(candidate)


def derive_local_time_band(local_datetime: datetime) -> str:
    """Map an hour-of-day to a coarse band the framer can reference."""
    h = local_datetime.hour
    if 5 <= h < 11:
        return "morning"
    if 11 <= h < 14:
        return "midday"
    if 14 <= h < 18:
        return "afternoon"
    if 18 <= h < 22:
        return "evening"
    return "late"
