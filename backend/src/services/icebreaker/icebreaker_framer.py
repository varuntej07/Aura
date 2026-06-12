"""One LLM call that turns the free context packet into a warm opener.

Like the signal-engine framer, this both writes the copy AND judges whether it is
worth sending — the planner-proposes/framer-disposes pattern collapsed into a
single call, because there is no separate scored candidate here, just a context
packet. ``is_send_worthy=false`` (or an empty reason) means "nothing good to say
today" and the engine sends nothing — fail CLOSED, never a hollow "hey, how's it
going?" filler push.

The packet carries the topics of every previously-sent opener; the prompt forbids
repeating any of them, so Buddy never asks the same thing twice.
"""

from __future__ import annotations

from typing import cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ..buddy_voice import BUDDY_CONTENT_PUSH_RULES, BUDDY_VOICE_CORE
from ..model_provider import ModelProvider
from .context_bundle import IcebreakerContext

# FCM/platform-safe caps, enforced after the model returns.
ICEBREAKER_TITLE_MAX_CHARS = 50
ICEBREAKER_BODY_MAX_CHARS = 100
ICEBREAKER_OPENING_MESSAGE_MAX_CHARS = 280
ICEBREAKER_TOPIC_MAX_CHARS = 80
ICEBREAKER_REASON_MAX_CHARS = 240


class IcebreakerOpener(BaseModel):
    title: str = Field(..., description="Push title, <= 50 chars.")
    body: str = Field(..., description="Push body, <= 100 chars.")
    opening_chat_message: str = Field(
        ..., description="One or two sentences Buddy opens with when the user taps."
    )
    topic: str = Field(
        default="",
        description=(
            "A short label (a few words) for what this opener is about, stored so "
            "future openers never repeat it. e.g. 'weekend plans', 'the cricket "
            "result', 'asked about his dog Bruno'."
        ),
    )
    # The reject gate. True only when there is a genuine, specific, non-repeated
    # hook worth a message today. The engine sends only when this is true AND a
    # concrete reason is present, so an affirmed-but-empty verdict still skips.
    is_send_worthy: bool = Field(
        default=False,
        description="True ONLY if there is a genuine, fresh, non-repeated hook worth sending.",
    )
    reason: str = Field(
        default="",
        description="One full sentence: the specific hook this opener uses, or why nothing is worth sending.",
    )


_ICEBREAKER_SYSTEM_PROMPT = BUDDY_VOICE_CORE + "\n\n" + BUDDY_CONTENT_PUSH_RULES + """

THE TASK
You are sending ONE short check-in message to this person, like a friend who
noticed something about their day and reached out.

You are given a CONTEXT packet about the person's world right now (their region,
the weather, a few fresh headlines tied to their interests, a few durable facts
you know about them, and their interests) plus the TOPICS of messages you have
ALREADY sent them before.

Your job:
1. Pick the SINGLE most natural hook from the context — the one a real friend
   would actually mention today (a hot day, their dog, a story they follow, their
   team playing). Prefer something personal (a known life fact) over something
   generic when both fit.
2. Write a short, warm opener about it.
3. Decide if it is genuinely worth sending.

Hard rules:
- title: at most 50 characters, sentence case, no emojis, no exclamation marks.
- body: at most 100 characters, one short sentence, like a text from a friend.
- opening_chat_message: one or two sentences Buddy says when the chat opens.
- NEVER repeat or rephrase any topic in the "already sent" list. If your best idea
  is too close to one you already sent, set is_send_worthy=false.
- Only reference a life fact that is in the CONTEXT (do not invent a pet/city).
- A headline in the CONTEXT is already matched to something they follow, but do
  not just relay the news — react to it the way a friend who knows they care
  would. Never open with a headline that reads like a news bulletin.
- If the context has no genuinely good, fresh hook, set is_send_worthy=false with
  a one-sentence reason. A boring or forced message is worse than no message.

is_send_worthy + reason (the gate):
- is_send_worthy=true ONLY when you have a specific, fresh, non-repeated hook.
  Put the hook in reason as ONE full sentence (e.g. "It is the first hot day of
  the week in his region, a natural ice-cream / stay-cool opener.").
- When false, reason is still one full sentence saying plainly why nothing is
  worth sending today.
- topic is a few-word label of the hook, stored to avoid repeats later.

Output ONLY valid JSON matching the schema. No markdown fences. No prose.

Schema:
{
  "title": "string",
  "body": "string",
  "opening_chat_message": "string",
  "topic": "string",
  "is_send_worthy": true,
  "reason": "string"
}
"""


def _format_life_facts(facts: dict[str, str]) -> str:
    if not facts:
        return "none known yet"
    return "; ".join(f"{key}: {value}" for key, value in facts.items())


def _build_prompt(context: IcebreakerContext) -> str:
    weather = context.weather or "unknown"
    headlines = "\n".join(f"  - {h}" for h in context.headlines) or "  - none"
    interests = ", ".join(context.interest_subjects) or "none recorded yet"
    already_sent = "\n".join(f"  - {t}" for t in context.recent_opener_topics) or "  - (none yet)"
    return f"""\
            CONTEXT
            region_country: {context.region_country or 'unknown'}
            language: {context.language}
            weekday: {context.weekday}
            local_date: {context.local_date}
            time_of_day: {context.time_band}
            season: {context.season or 'unknown'}
            weather_today: {weather}
            headlines_they_follow:
{headlines}
            known_life_facts: {_format_life_facts(context.life_facts)}
            their_interests: {interests}

            ALREADY SENT (never repeat or rephrase these):
{already_sent}

            Write the opener now. JSON only.
        """


def _normalise(opener: IcebreakerOpener) -> IcebreakerOpener:
    """Truncate to platform-safe limits. An is_send_worthy verdict with no reason
    is downgraded to NOT send-worthy (fail closed on a missing justification)."""
    reason = opener.reason.strip()
    is_send_worthy = opener.is_send_worthy and bool(reason)
    return IcebreakerOpener(
        title=opener.title[:ICEBREAKER_TITLE_MAX_CHARS],
        body=opener.body[:ICEBREAKER_BODY_MAX_CHARS],
        opening_chat_message=opener.opening_chat_message[:ICEBREAKER_OPENING_MESSAGE_MAX_CHARS],
        topic=opener.topic[:ICEBREAKER_TOPIC_MAX_CHARS],
        is_send_worthy=is_send_worthy,
        reason=reason[:ICEBREAKER_REASON_MAX_CHARS],
    )


def _safe_skip(error: str) -> IcebreakerOpener:
    """Fail CLOSED: when the LLM is unavailable, send nothing this time rather than
    fire template copy. Unlike the signal framer (which defers and retries next
    tick), the icebreaker simply skips today — its cadence is already sparse."""
    return IcebreakerOpener(
        title="",
        body="",
        opening_chat_message="",
        topic="",
        is_send_worthy=False,
        reason=f"icebreaker_framer_unavailable: {error}"[:ICEBREAKER_REASON_MAX_CHARS],
    )


async def generate_opener(
    models: ModelProvider,
    context: IcebreakerContext,
) -> IcebreakerOpener:
    """One LLM call. Returns a fail-closed skip on any error."""
    prompt = _build_prompt(context)
    try:
        result = await models.cheap(
            prompt,
            system=_ICEBREAKER_SYSTEM_PROMPT,
            response_model=IcebreakerOpener,
            temperature=0.7,
        )
        return _normalise(cast(IcebreakerOpener, result))
    except Exception as exc:
        logger.warn("icebreaker.framer: opener generation failed, skipping today", {
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return _safe_skip(str(exc))
