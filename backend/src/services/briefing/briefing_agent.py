"""The BriefingAgent — turns the buzzing-across-categories selection into a short,
scannable, Buddy-voice news briefing.

One ``models.cheap`` (Gemini Flash) call per user per day:
  1. The candidate_selector picks ~7-10 buzzing items spanning 3-4 categories (works
     for every user, vector or not).
  2. The model writes each one up as a short warm blurb in Buddy's voice and drops any
     it cannot ground or that lacks substance, returning the exact input index it used
     so the on-screen citation maps to a real source.

Never raises into the tick: any failure or an empty result returns ``None`` and the
caller marks the day skipped (no push).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast

from pydantic import BaseModel, Field

from ...lib.logger import logger
from ..buddy_voice import BUDDY_VOICE_CORE
from ..firebase import admin_firestore
from ..model_provider import ModelProvider
from ..signal_engine.notification_framer import derive_local_time_band
from ..user_aura_schema import (
    active_category_slugs,
    category_label,
    top_interest_subjects,
)
from .candidate_selector import SelectedItem, select_briefing_items
from .briefing_store import BriefingTargeting
from .world_region import resolve_region

ITEM_BLURB_MAX_CHARS = 320
CHAT_SEED_MAX_CHARS = 280
PUSH_TITLE_MAX_CHARS = 50
PUSH_BODY_MAX_CHARS = 100

DEFAULT_PUSH_TITLE = "Your briefing's in"
DEFAULT_PUSH_BODY = "I caught you up on your world. Peek?"

ITEM_BODY_CHARS = 400


@dataclass
class BriefingResult:
    # Each: {text, citation, category}. text is the warm blurb; citation indexes into
    # sources (1:1, parallel arrays); category is a human label for the on-screen chip.
    items: list[dict[str, Any]]
    narrative: str  # blurbs joined, kept for back-compat / plain-text rendering
    chat_seed_message: str
    push_title: str
    push_body: str
    sources: list[dict[str, Any]] = field(default_factory=list)


class _BriefingUserContext(BaseModel):
    name: str | None = None
    top_interests: list[str] = Field(default_factory=list)
    has_specific_interests: bool = True
    language: str = "English"
    local_time_band: str = "morning"


class _BriefingItemDraft(BaseModel):
    source_index: int = Field(..., description="1-based index of the input item this blurb is for.")
    blurb: str = Field(..., description="Short warm 2-3 line write-up, grounded only in that item.")


class BriefingDraft(BaseModel):
    """Structured output of the single briefing LLM call."""

    items: list[_BriefingItemDraft] = Field(default_factory=list)
    chat_seed_message: str = Field(default="", description="Opener naming a few items for the chat handoff.")
    push_title: str = Field(default="", description="Push title, <=50 chars, in Buddy's voice.")
    push_body: str = Field(default="", description="Push body, <=100 chars, opens a curiosity loop.")


_BRIEFING_SYSTEM_PROMPT = f"""\
{BUDDY_VOICE_CORE}

THE TASK
You are writing this person's daily news briefing: a quick scan of what is buzzing in
the world today, in your own voice, like a friend catching them up over coffee. You are
GIVEN a numbered list of real, current items across several categories. Write up the
ones worth knowing as short separate blurbs.

HARD GROUNDING RULES (these stop you making things up):
- Use ONLY facts present in the given item. Never invent a score, number, quote, name,
  date, or outcome that is not in the item text.
- Write one blurb per item you include, each keyed to that item's number.
- Drop any item with no real substance (e.g. only engagement counts, no article text)
  and any you cannot write honestly from its text. Keep the strong ones.

VOICE AND FORMAT (per blurb):
- 2 to 3 short lines. React like a friend who finds it interesting, do not relay a raw
  headline. Do NOT start with a dash, bullet, number, or the source name.
- Never name the source or platform (no "Hacker News", "Google News", "arXiv").
- No em-dashes, en-dashes, or double hyphens. No exclamation marks. No emoji pile-ons.

chat_seed_message: one or two sentences (<=250 chars) naming a few items concretely
("the Verstappen win, that small-model paper, and the cricket chase") and inviting them
to go deeper. Do NOT end with "thoughts?" or "what do you think?".

push_title (<=50 chars) and push_body (<=100 chars): warm, in your voice, opening a
curiosity loop without giving it away. Same NEVER rules (no source names, no exclamation
marks, no em-dashes).

Output ONLY valid JSON matching the schema. No markdown fences, no prose.

Schema:
{{
  "items": [{{"source_index": 1, "blurb": "string"}}],
  "chat_seed_message": "string",
  "push_title": "string",
  "push_body": "string"
}}
"""


def _build_user_context(
    aura: dict[str, Any],
    targeting: BriefingTargeting,
    local_now: datetime,
) -> _BriefingUserContext:
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


def _build_prompt(items: list[SelectedItem], ctx: _BriefingUserContext) -> str:
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
        "ITEMS (numbered; write up the ones worth knowing, drop the rest):",
    ]
    for i, item in enumerate(items, start=1):
        body = (item.body or "").strip()[:ITEM_BODY_CHARS]
        lines.append(f"{i}. category: {item.category} | title: {item.title}\n   body: {body}")
    lines.append("")
    lines.append("Write the briefing now. JSON only.")
    return "\n".join(lines)


async def _read_user_aura(user_id: str) -> dict[str, Any]:
    def _fetch() -> dict[str, Any]:
        snap = admin_firestore().collection("UserAura").document(user_id).get()
        return (snap.to_dict() or {}) if snap.exists else {}
    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:
        logger.warn("briefing.agent: UserAura read failed", {"user_id": user_id, "error": str(exc)})
        return {}


def _build_items_and_sources(
    selected: list[SelectedItem], drafts: list[_BriefingItemDraft]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Map the model's 1-based source_index back to (items, sources) parallel arrays.

    Invalid/duplicate indexes and empty blurbs are dropped, so a hallucinated index can
    never inject a phantom source. citation is the item's own position in sources.
    """
    seen: set[int] = set()
    items: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for draft in drafts:
        idx = draft.source_index
        if not isinstance(idx, int) or idx < 1 or idx > len(selected) or idx in seen:
            continue
        blurb = (draft.blurb or "").strip()[:ITEM_BLURB_MAX_CHARS]
        if not blurb:
            continue
        seen.add(idx)
        cand = selected[idx - 1]
        citation = len(sources)
        sources.append({
            "title": cand.title,
            "url": cand.url,
            "source": cand.source,
            "category": cand.category,
        })
        items.append({
            "text": blurb,
            "citation": citation,
            "category": category_label(cand.category),
        })
    return items, sources


async def generate(
    models: ModelProvider,
    user_id: str,
    targeting: BriefingTargeting,
    local_now: datetime,
) -> BriefingResult | None:
    """Produce today's briefing, or None when there is nothing worth sending."""
    resolved = resolve_region(targeting.timezone)
    region = None if resolved.is_global else resolved.country_code
    selected = await select_briefing_items(user_id, region=region, now=local_now)
    if not selected:
        logger.info("briefing.agent: no candidates selected, skipping", {"user_id": user_id})
        return None

    aura = await _read_user_aura(user_id)
    ctx = _build_user_context(aura, targeting, local_now)
    prompt = _build_prompt(selected, ctx)

    try:
        result = await asyncio.wait_for(
            models.cheap(prompt, system=_BRIEFING_SYSTEM_PROMPT, response_model=BriefingDraft, temperature=0.6),
            timeout=15.0,
        )
        draft = cast(BriefingDraft, result)
    except Exception as exc:
        logger.warn("briefing.agent: LLM generation failed", {
            "user_id": user_id, "error": str(exc), "error_type": type(exc).__name__,
        })
        return None

    items, sources = _build_items_and_sources(selected, draft.items)
    if not items:
        logger.info("briefing.agent: model kept no items (nothing relevant)", {"user_id": user_id})
        return None

    narrative = "\n\n".join(item["text"] for item in items)
    chat_seed = (draft.chat_seed_message or "").strip()[:CHAT_SEED_MAX_CHARS]
    push_title = (draft.push_title or "").strip()[:PUSH_TITLE_MAX_CHARS] or DEFAULT_PUSH_TITLE
    push_body = (draft.push_body or "").strip()[:PUSH_BODY_MAX_CHARS] or DEFAULT_PUSH_BODY

    logger.info("briefing.agent: briefing generated", {
        "user_id": user_id,
        "items_considered": len(selected),
        "items_used": len(items),
        "categories": len({s["category"] for s in sources}),
    })
    return BriefingResult(
        items=items,
        narrative=narrative,
        chat_seed_message=chat_seed,
        push_title=push_title,
        push_body=push_body,
        sources=sources,
    )
