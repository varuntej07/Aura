"""The BriefingAgent — the "middle man" that turns the ranked content pool into one
synthesized, Buddy-voice morning digest.

It does three things in one pass:
  1. PICK   — pull the user's top-ranked pool items via the signal engine's existing
              ``rank_session`` (the same ranking the notifications use; this is its
              first in-app consumer). No new fetching, no new ranking math.
  2. JUDGE  — a single grounded LLM call decides which of those items genuinely fit
              this person and silently drops the weak/substanceless ones (the
              "sees if it is relevant" requirement). The model returns the exact
              item indexes it used, so the on-screen sources are auditable.
  3. WRITE  — the same call synthesizes a flowing, grounded narrative plus a short
              opener that seeds the "Chat about this" chat.

One ``models.cheap`` (Gemini Flash) call per user per day. Never raises into the
tick: any failure or an empty result returns ``None`` and the caller marks the day
skipped (no push).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, Field

from ...config.settings import settings
from ...lib.logger import logger
from ..buddy_voice import BUDDY_VOICE_CORE
from ..firebase import admin_firestore
from ..model_provider import ModelProvider
from ..signal_engine.notification_framer import derive_local_time_band
from ..signal_engine.recommender import rank_session
from ..user_aura_schema import (
    active_category_slugs,
    category_label,
    top_interest_subjects,
)
from .briefing_store import BriefingTargeting

# Hard caps applied after the model returns (the prompt asks for these too, but the
# model occasionally overshoots; truncation guarantees a sane document + push).
NARRATIVE_MAX_CHARS = 1200
CHAT_SEED_MAX_CHARS = 280
PUSH_TITLE_MAX_CHARS = 50
PUSH_BODY_MAX_CHARS = 100

# Templated fallbacks used only if the model leaves a push field blank — the push
# must never go out empty.
DEFAULT_PUSH_TITLE = "Your briefing's in"
DEFAULT_PUSH_BODY = "I caught you up on your world. Peek?"

# How much of each item body to show the model. Enough to ground the narrative
# without bloating the single prompt.
ITEM_BODY_CHARS = 400


@dataclass
class BriefingResult:
    narrative: str
    chat_seed_message: str
    push_title: str
    push_body: str
    # Each: {title, url, source, category} — exactly the items the model wove in.
    sources: list[dict[str, Any]] = field(default_factory=list)


class _BriefingUserContext(BaseModel):
    name: str | None = None
    top_interests: list[str] = Field(default_factory=list)
    has_specific_interests: bool = True
    language: str = "English"
    local_time_band: str = "morning"


class BriefingDraft(BaseModel):
    """Structured output of the single briefing LLM call."""

    narrative: str = Field(..., description="The synthesized on-screen briefing.")
    chat_seed_message: str = Field(
        ...,
        description="Short opener naming the items, used when the user taps 'Chat about this'.",
    )
    used_item_indexes: list[int] = Field(
        default_factory=list,
        description="1-based indexes of the input items actually referenced.",
    )
    push_title: str = Field(
        default="",
        description="Push notification title, at most 50 chars, in Buddy's voice.",
    )
    push_body: str = Field(
        default="",
        description="Push notification body, at most 100 chars, opens a curiosity loop.",
    )


_BRIEFING_SYSTEM_PROMPT = f"""\
{BUDDY_VOICE_CORE}

THE TASK
You are writing this one person's daily briefing: a short, warm "here is what is up
in your world today" from Buddy. You are GIVEN a numbered list of real items pulled
for this user. Weave the ones that genuinely fit them into one flowing briefing in
your own voice, like a close friend catching them up over coffee.

HARD GROUNDING RULES (these prevent you from making things up):
- Use ONLY facts that appear in the provided items. Do NOT invent scores, numbers,
  quotes, names, dates, outcomes, or any detail not present in the item text.
- Do NOT add any topic or item that is not in the list.
- Weave in only the items that genuinely match this user's interests. Silently drop
  weak ones, off-topic ones, and any whose body has no real substance (e.g. only
  engagement counts like "120 points, 60 comments", no article text).
- If fewer than two items are worth including, write a SHORTER briefing about just
  what fits. If NONE fit, return an empty narrative and an empty used_item_indexes.

VOICE AND FORMAT:
- A few short paragraphs (not a bulleted list, not a single run-on). Conversational.
- React to each thing like a friend who knows they care, do not relay raw headlines.
- Never name the source or platform (no "Hacker News", "Google News", "arXiv").
- No em-dashes, en-dashes, or double hyphens. No emoji pile-ons. No exclamation marks.
- You MAY use their name once if it lands naturally; do not force it.
- narrative: at most ~1000 characters.
- chat_seed_message: one or two sentences, at most ~250 characters. It is what Buddy
  says if the user taps to chat about the briefing, so name the items concretely
  ("the Verstappen win, that small-model paper, and the cricket chase") and invite
  them to go deeper. Do NOT end with "what do you think?" or "thoughts?".
- used_item_indexes: the EXACT 1-based indexes of the items you referenced in the
  narrative. This is an audit trail and powers the on-screen sources list, so it
  must list every item you used and nothing you did not.
- push_title: the notification title, at most 50 chars, warm and in your voice (not
  "Daily briefing"). push_body: at most 100 chars, one line that opens a curiosity
  loop about what is inside without giving it away ("peek?", "worth two minutes").
  Both must obey the same NEVER rules (no source names, no exclamation marks, no
  em-dashes). If the narrative is empty, leave both push fields empty.

Output ONLY valid JSON matching the schema. No markdown fences. No prose.

Schema:
{{
  "narrative": "string",
  "chat_seed_message": "string",
  "used_item_indexes": [1, 2],
  "push_title": "string",
  "push_body": "string"
}}
"""


def _build_user_context(
    aura: dict[str, Any],
    targeting: BriefingTargeting,
    local_now: datetime,
) -> _BriefingUserContext:
    """Assemble the small voice context. Specific learned subjects make the briefing
    feel personal; a cold-start user with only declared categories falls back to
    category labels (mirrors notification_framer._build_framing_context)."""
    subjects = top_interest_subjects(aura, k=5)
    has_specific = bool(subjects)
    if has_specific:
        interests = subjects
    else:
        slugs = active_category_slugs(aura)
        interests = [category_label(s) for s in slugs[:5]]
    return _BriefingUserContext(
        name=targeting.display_name,
        top_interests=interests,
        has_specific_interests=has_specific,
        language=targeting.language or "English",
        local_time_band=derive_local_time_band(local_now),
    )


def _build_prompt(items: list[Any], ctx: _BriefingUserContext) -> str:
    interest_kind = (
        "specific subjects they care about"
        if ctx.has_specific_interests
        else "broad areas they picked at signup, no specific subjects learned yet"
    )
    interests_line = ", ".join(ctx.top_interests[:5]) if ctx.top_interests else "none recorded yet"
    lines = [
        "USER CONTEXT",
        f"name: {ctx.name or 'unknown'}",
        f"top_interests ({interest_kind}): {interests_line}",
        f"language: {ctx.language}",
        f"local_time_band: {ctx.local_time_band}",
        "",
        "ITEMS (numbered; use only these):",
    ]
    for i, item in enumerate(items, start=1):
        body = (item.body or "").strip()[:ITEM_BODY_CHARS]
        lines.append(
            f"{i}. category: {item.category} | title: {item.title}\n   body: {body}"
        )
    lines.append("")
    lines.append("Write the briefing now. JSON only.")
    return "\n".join(lines)


async def _read_user_aura(user_id: str) -> dict[str, Any]:
    """Read UserAura/{uid} once for the voice context. Consent was already confirmed
    by the engine before generate() is called. Returns {} on miss/error."""
    def _fetch() -> dict[str, Any]:
        snap = admin_firestore().collection("UserAura").document(user_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("briefing.agent: UserAura read failed", {"user_id": user_id, "error": str(exc)})
        return {}


def _sources_from_indexes(items: list[Any], used_indexes: list[int]) -> list[dict[str, Any]]:
    """Map the model's 1-based used indexes back to {title, url, source, category}.
    Invalid/duplicate indexes are dropped so a hallucinated index can never inject a
    phantom source."""
    seen: set[int] = set()
    sources: list[dict[str, Any]] = []
    for idx in used_indexes:
        if not isinstance(idx, int) or idx < 1 or idx > len(items) or idx in seen:
            continue
        seen.add(idx)
        item = items[idx - 1]
        sources.append({
            "title": item.title,
            "url": item.url,
            "source": item.source,
            "category": item.category,
        })
    return sources


async def generate(
    models: ModelProvider,
    user_id: str,
    targeting: BriefingTargeting,
    local_now: datetime,
) -> BriefingResult | None:
    """Produce today's briefing, or None when there is nothing worth sending.

    A None return means the caller marks the day skipped (no push): either the pool
    had no ranked items for this user, or the model judged none of them relevant.
    """
    items = await rank_session(user_id, limit=settings.BRIEFING_CANDIDATE_POOL)
    if not items:
        # Cold-start user with no interest vector yet (the signal tick bootstraps it
        # from UserAura on its own cadence). Skip today rather than synthesize a weak
        # briefing from nothing; recovers automatically once a vector exists.
        logger.info("briefing.agent: no ranked items, skipping", {"user_id": user_id})
        return None

    aura = await _read_user_aura(user_id)
    ctx = _build_user_context(aura, targeting, local_now)
    prompt = _build_prompt(items, ctx)

    try:
        result = await asyncio.wait_for(
            models.cheap(prompt, system=_BRIEFING_SYSTEM_PROMPT, response_model=BriefingDraft, temperature=0.6),
            timeout=15.0,
        )
        draft = cast(BriefingDraft, result)
    except Exception as exc:
        logger.warn("briefing.agent: LLM generation failed", {
            "user_id": user_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return None

    narrative = (draft.narrative or "").strip()[:NARRATIVE_MAX_CHARS]
    if not narrative:
        # The model judged nothing relevant. Skip — never send an empty briefing.
        logger.info("briefing.agent: model returned empty narrative (nothing relevant)", {
            "user_id": user_id,
        })
        return None

    chat_seed = (draft.chat_seed_message or "").strip()[:CHAT_SEED_MAX_CHARS]
    push_title = (draft.push_title or "").strip()[:PUSH_TITLE_MAX_CHARS] or DEFAULT_PUSH_TITLE
    push_body = (draft.push_body or "").strip()[:PUSH_BODY_MAX_CHARS] or DEFAULT_PUSH_BODY
    sources = _sources_from_indexes(items, draft.used_item_indexes)

    logger.info("briefing.agent: briefing generated", {
        "user_id": user_id,
        "items_considered": len(items),
        "sources_used": len(sources),
        "narrative_len": len(narrative),
    })
    return BriefingResult(
        narrative=narrative,
        chat_seed_message=chat_seed,
        push_title=push_title,
        push_body=push_body,
        sources=sources,
    )
