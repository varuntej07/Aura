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
from ..buddy_voice import BUDDY_VOICE_CORE
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


_FRAMER_SYSTEM_PROMPT = f"""\
{BUDDY_VOICE_CORE}

THE TASK
You just remembered something this person mentioned and you are genuinely curious
about it. Ask ONE warm, specific question to understand it better, the way a friend
who actually cares would. The whole point of the message is to earn one more true
thing about them, never to check up on a task.

Rules, all hard:
- Curiosity, never accountability. NEVER ask "did you finish / complete / do it /
  get it done", and NEVER frame it as them forgetting, slacking, keeping up, or
  staying on top of something. You are intrigued, not checking up. Ask what it is,
  who it is for, how they feel about it, the story.
- Name the SPECIFIC thing they mentioned, in their words. A question that names
  nothing concrete is a failure: it reads as nagging and gives them nothing to grab.
  Point straight at the actual subject so it is obvious what you are asking about.
- Short: the body is at most 90 characters and sounds like a text from a friend.
  Lowercase is fine.
- suggested_replies: 2 or 3 options that make it effortless to START sharing. They
  are conversation-openers, not yes/no, and never progress states. Each is at most
  24 characters.
- At most one emoji across the whole message. No exclamation pile-ons. Never open
  with "I noticed that you".
- Output ONLY valid JSON matching the schema. No markdown fences. No prose.

Schema:
{{
  "title": "string",
  "body": "string",
  "suggested_replies": ["string", "string"]
}}
"""

# Few-shot examples steer the tone hard away from "teacher" and toward "friend".
_FEW_SHOT = """\
EXAMPLES

mentioned: "implement live fetch instead of stale cache in my feature"
unknown: ["what the project is"]
-> {"title":"that thing you're building",
    "body":"what are you making that needs live data over cache?",
    "suggested_replies":["a side project","for work","i'll show you"]}

mentioned: "big presentation monday"
unknown: ["what it's about","how they feel about it"]
-> {"title":"monday",
    "body":"what's the presentation on? you feeling ready or nah",
    "suggested_replies":["kinda nervous","i got this","long story"]}

mentioned: "follows cricket a lot"   (source: aura_gap)
unknown: ["which team they support"]
-> {"title":"quick one",
    "body":"who's your team? gotta know who you're suffering for",
    "suggested_replies":["RCB","CSK","just love the game"]}

NEVER WRITE LIKE THIS:

mentioned: "call the bank about the lease deposit"
-> {"title":"what's the deal",
    "body":"you always forgetting or just trying to stay on top of it",
    "suggested_replies":["...","..."]}
   names nothing specific and accuses them of forgetting. That is the nagging,
   accountability voice this prompt exists to kill. Name the bank call and be curious
   about it instead: "what's the bank thing about? sorting the lease?"
"""


def _build_prompt(thread: Thread, ctx: FollowUpFramingContext) -> str:
    interests_line = (
        ", ".join(ctx.top_interests[:3])
        if ctx.top_interests else "no strong interests recorded yet"
    )
    unknown_line = "; ".join(thread.unknown) if thread.unknown else "anything about it"
    return f"""\
{_FEW_SHOT}

NOW WRITE FOR THIS ONE.

PERSON
tone they like: {ctx.dominant_tone or "neutral"}
depth level (1-5): {ctx.depth_level}
top interests: {interests_line}
local time band: {ctx.time_band}

THREAD
they mentioned: "{thread.trigger_text}"
source: {thread.source}
what I already know: {thread.known_summary or "not much yet"}
what I don't know yet: {unknown_line}

Pick the single most interesting unknown and ask about THAT. JSON only.
"""


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
            system=_FRAMER_SYSTEM_PROMPT,
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
