"""
Outbound drafter: turns a screen frame + spoken intent into ONE ready-to-send
message written in the user's voice, or (channel ``snippet``) a copy-exact
command/code/config snippet where the frame is optional. The brain behind the
voice agent's ``draft_outbound_message`` tool and ``POST /desktop/draft-outbound/refine``
(``handlers/draft_outbound.py``). Desktop sibling of ``services/keyboard/drafter.py``.

Memory CONSUMER, never a producer. Callers pass a compact ``voice_lines`` digest
(built here via :func:`writing_voice_lines` from the consent-gated UserAura
profile) so the draft sounds like the user. This module itself persists
nothing; its CALLERS persist the resulting draft text and context summary to
``UserAura/{uid}/drafts`` for the dashboard (``services/drafts/store.py``).
The screen frame never leaves the call frame and is never stored.

Never raises. A timeout, a model failure, or an invalid request each return an
empty result with a coded reason, so the voice agent degrades to a spoken
sentence and the desktop card shows graceful copy instead of hanging.
"""

from __future__ import annotations

from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from ...lib.logger import logger
from ...prompts import (
    TONE_DESCRIPTIONS,
    outbound_draft_system_prompt,
    outbound_draft_user_prompt,
    outbound_refine_user_prompt,
)
from .. import draft_common
from ..model_provider import get_model_provider
from ..user_aura_schema import interest_prompt_lines
from .skills import GENERAL_SKILL_ID, get_writing_skill, is_writing_skill_id

Channel = Literal["on_screen", "email_reply", "cold_dm", "snippet"]
Length = Literal["short", "medium", "detailed"]

# "on_screen" is the general, adaptive default: the vision model reads the
# screenshot to see what's being written and where it goes (a form or
# application field, a message box, an email, a comment, a bio, a post), then
# writes exactly that text as the user. It carries the user's writing voice
# like the outbound channels but ignores the length ladder (length is inferred
# from the field/intent), so there is never a "how long?" question. This is the
# channel the voice tool falls back to whenever the model doesn't (or can't)
# name a more specific one, which kills the old "email or new message?" loop.
DEFAULT_CHANNEL = "on_screen"

# "snippet" is the copy-exact channel: terminal commands, code, config. Unlike
# the outbound channels it needs no screen frame (the spoken intent is often
# the whole spec) and ignores the length ladder and the user's writing voice.
SNIPPET_CHANNEL = "snippet"

CHANNELS: frozenset[str] = frozenset(
    {DEFAULT_CHANNEL, "email_reply", "cold_dm", SNIPPET_CHANNEL}
)
LENGTHS: frozenset[str] = frozenset({"short", "medium", "detailed"})

# Channels that carry the user's writing voice AND infer their own length from
# the screen/intent instead of the length ladder (so the caller never has to
# supply or ask for a length). "snippet" also skips the ladder but carries no
# persona; it is handled separately.
_ADAPTIVE_LENGTH_CHANNELS: frozenset[str] = frozenset({DEFAULT_CHANNEL, SNIPPET_CHANNEL})

# Coded reasons: the caller maps every one of these to graceful speech/UI
# copy. Loud, never silent. The three shared with the keyboard drafter live in
# services/draft_common.py (one wire vocabulary); re-exported here so clients
# keep reading drafter.REASON_*.
REASON_OK = draft_common.REASON_OK
REASON_TIMEOUT = draft_common.REASON_TIMEOUT
REASON_MODEL_ERROR = draft_common.REASON_MODEL_ERROR
REASON_NO_FRAME = "no_frame"
REASON_INVALID = "invalid_request"

# Hard ceilings per call. The initial draft runs on the expert tier with a frame
# image (reading a dense email thread accurately IS the feature, and volume is
# capped by the free-tier daily counter), so it gets a generous budget. Refines
# are text-only transforms on the balanced tier and must feel snappy on a chip tap.
DRAFT_TIMEOUT_SECONDS = 25.0
REFINE_TIMEOUT_SECONDS = 10.0

# Defensive input caps: cost + latency guards against a runaway payload. The
# context summary is model-written (2-4 sentences), the hints come from spoken
# words, and the prior draft tops out around a "detailed" length.
CONTEXT_SUMMARY_MAX_CHARS = 1200
PRIOR_DRAFT_MAX_CHARS = 4000
HINT_MAX_CHARS = 500
VOICE_LINES_MAX = 6


class _GeneratedArtifact(BaseModel):
    """Exact copyable content. No conversational prose belongs beside it."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=PRIOR_DRAFT_MAX_CHARS)


class _PrivateDraftContext(BaseModel):
    """Server-only context used for later refinement, never rendered."""

    model_config = ConfigDict(extra="forbid")

    summary: str = Field(default="", max_length=CONTEXT_SUMMARY_MAX_CHARS)


class _DraftOutput(BaseModel):
    """Strict initial-generation shape parsed by the provider."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    artifact: _GeneratedArtifact
    private_context: _PrivateDraftContext


class _RefineOutput(BaseModel):
    """Strict refinement shape containing only the replacement body."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    artifact: _GeneratedArtifact


class OutboundDraftResult(BaseModel):
    text: str = ""
    context_summary: str = ""
    reason: str = REASON_OK


def writing_voice_lines(profile: dict[str, Any]) -> list[str]:
    """Compact writing-voice digest from a UserAura profile.

    One tone line (when the extractor has settled on a dominant tone) plus a few
    interest lines so a draft can sound genuinely theirs. Callers obtain
    ``profile`` via the consent-gated ``fetch_cached_aura_data``; an empty
    profile yields an empty list, which triggers the default-voice paragraph in
    the prompt instead.
    """
    if not profile:
        return []
    lines: list[str] = []
    dominant_tone = profile.get("dominant_tone")
    if dominant_tone in TONE_DESCRIPTIONS:
        lines.append(f"Their natural register is {TONE_DESCRIPTIONS[dominant_tone]}.")
    try:
        lines.extend(interest_prompt_lines(profile))
    except Exception as exc:
        logger.warn("outbound_draft: interest lines failed", {"error": str(exc)})
    return lines[:VOICE_LINES_MAX]


async def draft_outbound(
    uid: str,
    *,
    channel: str,
    length: str,
    recipient_hint: str,
    intent: str,
    jpeg_base64: str,
    jpeg_width: int | None,
    jpeg_height: int | None,
    voice_lines: list[str],
    display_name: str,
    skill_id: str = GENERAL_SKILL_ID,
) -> OutboundDraftResult:
    """Draft one outbound message or snippet. Never raises.

    A screen frame is mandatory for the outbound channels (the message IS a
    response to what's on screen) but optional for snippets, where the spoken
    intent alone is usually the whole spec. With a frame the call runs on the
    expert vision tier; a frameless snippet runs text-only on the balanced
    tier, which is snappier and cheaper.
    """
    # Adaptive-length channels (on_screen, snippet) infer length from the
    # screen/intent, so a blank length is valid for them; the outbound channels
    # still need one from the ladder.
    length_ok = length in LENGTHS or channel in _ADAPTIVE_LENGTH_CHANNELS
    if channel not in CHANNELS or not length_ok or not is_writing_skill_id(skill_id):
        return OutboundDraftResult(reason=REASON_INVALID)
    if (
        not jpeg_base64
        and channel != SNIPPET_CHANNEL
        and skill_id == GENERAL_SKILL_ID
    ):
        return OutboundDraftResult(reason=REASON_NO_FRAME)

    system_prompt = outbound_draft_system_prompt(
        channel=channel,
        length=length,
        voice_lines=voice_lines,
        voice_limit=VOICE_LINES_MAX,
    )
    skill = get_writing_skill(skill_id)
    system_prompt = f"{system_prompt}\n\nSelected writing skill:\n{skill.instructions}"
    user_prompt = outbound_draft_user_prompt(
        channel=channel,
        length=length,
        recipient_hint=recipient_hint.strip()[:HINT_MAX_CHARS],
        intent=intent,
        jpeg_width=jpeg_width if jpeg_base64 else None,
        jpeg_height=jpeg_height if jpeg_base64 else None,
        display_name=display_name.strip()[:HINT_MAX_CHARS],
        has_frame=bool(jpeg_base64),
    )

    # A message draft wants creative range; a snippet wants exactness.
    temperature = 0.2 if channel == SNIPPET_CHANNEL else 0.7

    provider = get_model_provider()
    if jpeg_base64:
        model_call = provider.expert(
            user_prompt,
            system=system_prompt,
            images=[{"media_type": "image/jpeg", "data": jpeg_base64}],
            response_model=_DraftOutput,
            temperature=temperature,
        )
    else:
        model_call = provider.balanced(
            user_prompt,
            system=system_prompt,
            response_model=_DraftOutput,
            temperature=temperature,
        )
    raw, reason = await draft_common.bounded_model_call(
        model_call,
        timeout_s=DRAFT_TIMEOUT_SECONDS,
        log_prefix="outbound_draft: draft",
        log_fields={"user_id": uid, "channel": channel},
    )
    if reason != REASON_OK:
        return OutboundDraftResult(reason=reason)
    result = cast(_DraftOutput, raw)

    text = result.artifact.body.strip()
    if not text:
        return OutboundDraftResult(reason=REASON_MODEL_ERROR)

    summary = result.private_context.summary.strip()[:CONTEXT_SUMMARY_MAX_CHARS]
    logger.info(
        "outbound_draft: draft ok",
        {
            "user_id": uid,
            "channel": channel,
            "length": length,
            "text_chars": len(text),
            "summary_chars": len(summary),
            "personalized": bool(voice_lines),
        },
    )
    return OutboundDraftResult(text=text, context_summary=summary, reason=REASON_OK)

async def refine_outbound(
    uid: str,
    *,
    channel: str,
    length: str,
    prior_draft: str,
    refine_instruction: str,
    context_summary: str,
    voice_lines: list[str],
    skill_id: str = GENERAL_SKILL_ID,
) -> OutboundDraftResult:
    """Rework an existing draft per the instruction. Text-only, never raises."""
    prior = prior_draft.strip()[:PRIOR_DRAFT_MAX_CHARS]
    instruction = refine_instruction
    length_ok = length in LENGTHS or channel in _ADAPTIVE_LENGTH_CHANNELS
    if (
        channel not in CHANNELS
        or not length_ok
        or not is_writing_skill_id(skill_id)
        or not prior
        or not instruction.strip()
    ):
        return OutboundDraftResult(reason=REASON_INVALID)

    summary = context_summary.strip()[:CONTEXT_SUMMARY_MAX_CHARS]
    system_prompt = outbound_draft_system_prompt(
        channel=channel,
        length=length,
        voice_lines=voice_lines,
        voice_limit=VOICE_LINES_MAX,
    )
    skill = get_writing_skill(skill_id)
    system_prompt = f"{system_prompt}\n\nSelected writing skill:\n{skill.instructions}"
    user_prompt = outbound_refine_user_prompt(
        channel=channel,
        length=length,
        prior_draft=prior,
        instruction=instruction,
        context=summary,
    )

    raw, reason = await draft_common.bounded_model_call(
        get_model_provider().balanced(
            user_prompt,
            system=system_prompt,
            response_model=_RefineOutput,
            temperature=0.2 if channel == SNIPPET_CHANNEL else 0.7,
        ),
        timeout_s=REFINE_TIMEOUT_SECONDS,
        log_prefix="outbound_draft: refine",
        log_fields={"user_id": uid, "channel": channel},
    )
    if reason != REASON_OK:
        return OutboundDraftResult(reason=reason)
    result = cast(_RefineOutput, raw)

    text = result.artifact.body.strip()
    if not text:
        return OutboundDraftResult(reason=REASON_MODEL_ERROR)

    logger.info(
        "outbound_draft: refine ok",
        {"user_id": uid, "channel": channel, "length": length, "text_chars": len(text)},
    )
    # The summary is returned unchanged so the client keeps a stable refine context.
    return OutboundDraftResult(text=text, context_summary=summary, reason=REASON_OK)
