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
from ...prompts import ICEBREAKER_SYSTEM_PROMPT, icebreaker_user_prompt
from ..model_provider import ModelProvider
from ..signal_engine.notification_framer import (
    strip_long_dashes,
    truncate_at_word_boundary,
)
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


def _format_life_facts(facts: dict[str, str]) -> str:
    if not facts:
        return "none known yet"
    return "; ".join(f"{key}: {value}" for key, value in facts.items())


def _build_prompt(context: IcebreakerContext) -> str:
    return icebreaker_user_prompt(
        region=context.region_country,
        language=context.language,
        weekday=context.weekday,
        local_date=context.local_date,
        time_band=context.time_band,
        season=context.season,
        weather=context.weather,
        headlines=context.headlines,
        life_facts=_format_life_facts(context.life_facts),
        interests=context.interest_subjects,
        recent_topics=context.recent_opener_topics,
    )


def _normalise(opener: IcebreakerOpener) -> IcebreakerOpener:
    """Truncate to platform-safe limits. An is_send_worthy verdict with no reason
    is downgraded to NOT send-worthy (fail closed on a missing justification)."""
    reason = opener.reason.strip()
    is_send_worthy = opener.is_send_worthy and bool(reason)
    return IcebreakerOpener(
        title=truncate_at_word_boundary(
            strip_long_dashes(opener.title), ICEBREAKER_TITLE_MAX_CHARS
        ),
        body=truncate_at_word_boundary(
            strip_long_dashes(opener.body), ICEBREAKER_BODY_MAX_CHARS
        ),
        opening_chat_message=truncate_at_word_boundary(
            strip_long_dashes(opener.opening_chat_message),
            ICEBREAKER_OPENING_MESSAGE_MAX_CHARS,
        ),
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
            system=ICEBREAKER_SYSTEM_PROMPT,
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
