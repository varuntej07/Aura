"""On-demand "Catch me up on the world" snapshot — the cold-start / refresh path.

Unlike the scheduled :mod:`briefing_agent` (which personalises from the user's ranked
content pool and needs an interest vector the user may not have yet), this is GENERAL
world news: 2-3 globally buzzing stories plus one local story for the user's region. It
exists so a brand-new user — who has no vector, so the scheduled digest is empty — still
has a live, useful action on the briefing screen, and so the follow-on chat bootstraps
their interest profile through the normal extractor.

ONE ``models.grounded`` (Gemini + Google Search) call does live search + synthesis. Two
caches keep it cheap and abuse-resistant:

  * per-REGION result cache — the world is the same for everyone in a region, so one
    grounded call serves every user there for ``WORLD_BRIEFING_CACHE_TTL_SECONDS``. This
    is what makes grounding affordable at scale.
  * per-USER force-refresh cooldown — a normal open serves the warm region cache for
    free; a force-refresh inside ``WORLD_BRIEFING_REFRESH_COOLDOWN_SECONDS`` is coalesced
    back to the cache, so no one can spam fresh generations from the refresh icon.

Both caches are in-process (per Cloud Run instance). At beta scale that is plenty; a
multi-instance deployment simply gets one grounded call per instance per region per
window, still bounded. Never raises into the caller: any failure returns ``None`` and the
handler answers ``{"briefing": null}`` so the screen shows its empty state.
"""

from __future__ import annotations

from ...prompts import WORLD_BRIEFING_SYSTEM_PROMPT

import re
import time
from dataclasses import dataclass, field
from typing import Any

from ...config.settings import settings
from ...lib.logger import logger
from ..model_provider import ModelProvider, get_model_provider
from .world_region import WorldRegion, resolve_region

# Output caps applied after the model returns (the prompt asks for these too, but a model
# can overshoot; truncation guarantees a sane document).
NARRATIVE_MAX_CHARS = 1200
CHAT_SEED_MAX_CHARS = 280
# Cap on news items rendered, so a chatty model can't produce a wall of blurbs.
MAX_ITEMS = 6

# Marker the model emits between the narrative and the chat opener (grounding rules out a
# JSON response_schema, so we parse a sentinel out of free text instead).
_CHAT_SEED_MARKER = "CHAT_SEED:"

# Used only if the model omits the opener — the seed must never be empty (it is what
# Buddy says when the user taps to chat).
_DEFAULT_CHAT_SEED = "Want me to go deeper on any of this?"


@dataclass
class WorldBriefingResult:
    narrative: str
    chat_seed_message: str
    region_code: str
    # Each {title, url, source, category}; mirrors the scheduled briefing's source shape
    # so the Flutter DailyBriefing model + screen render it unchanged.
    sources: list[dict[str, Any]] = field(default_factory=list)
    # Discrete news items the screen renders, each {text, citation}. ``text`` is one
    # short 2-3 line blurb; ``citation`` is the index into ``sources`` that grounds it
    # (or None). Lets the UI show a small per-item citation instead of a sources list.
    items: list[dict[str, Any]] = field(default_factory=list)


def _build_user_prompt(region: WorldRegion) -> str:
    if region.is_global:
        return "Local region: none — give global stories only.\n\nWrite the catch-up now."
    return (
        f"Local region for the one local story: {region.country_name}.\n\n"
        "Write the catch-up now."
    )


def _parse_output(text: str) -> tuple[str, str]:
    """Split the grounded free text into (narrative, chat_seed). Tolerant: if the marker
    is missing, the whole text is the narrative and a default opener is used, so a model
    that forgets the sentinel still yields a usable briefing."""
    raw = (text or "").strip()
    if not raw:
        return "", ""
    idx = raw.rfind(_CHAT_SEED_MARKER)
    if idx == -1:
        return raw, _DEFAULT_CHAT_SEED
    narrative = raw[:idx].strip()
    after = raw[idx + len(_CHAT_SEED_MARKER):].strip()
    chat_seed = after.splitlines()[0].strip() if after else ""
    return narrative, (chat_seed or _DEFAULT_CHAT_SEED)


def _sources_payload(raw_sources: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Reshape grounded {title,url} refs into the client's source schema, preserving
    ORDER and LENGTH 1:1 (no skipping) so a citation's source index stays valid. A
    citation only renders when its source url is non-empty (the client guards on that)."""
    out: list[dict[str, Any]] = []
    for src in raw_sources:
        url = (src.get("url") or "").strip()
        out.append({
            "title": (src.get("title") or url).strip(),
            "url": url,
            "source": "",
            "category": "",
        })
    return out


def _split_items(narrative: str) -> list[str]:
    """Split the narrative into discrete news items on blank lines (the prompt asks the
    model to separate stories that way). Capped at MAX_ITEMS so a chatty model can't
    flood the screen."""
    parts = [p.strip() for p in re.split(r"\n\s*\n", narrative) if p.strip()]
    return parts[:MAX_ITEMS]


def _assign_citations(
    items: list[str], supports: list[dict[str, Any]], n_sources: int
) -> list[int | None]:
    """Map each item to the source that grounds it. For each item, find a grounding
    support whose covered text appears in the item, and take the first source index it
    points at (clamped to the sources we kept). Substring matching avoids byte-offset
    fragility; an item with no matching support gets ``None`` (no citation shown)."""
    citations: list[int | None] = []
    for item in items:
        chosen: int | None = None
        for sup in supports:
            seg = (sup.get("text") or "").strip()
            if not seg or seg not in item:
                continue
            for idx in sup.get("source_indices", []):
                if isinstance(idx, int) and 0 <= idx < n_sources:
                    chosen = idx
                    break
            if chosen is not None:
                break
        citations.append(chosen)
    return citations


# In-process caches (see module docstring). Keyed by region code / user id.
_region_cache: dict[str, tuple[float, WorldBriefingResult]] = {}
_user_refresh_at: dict[str, float] = {}


def _region_cache_get(region_code: str, now: float) -> WorldBriefingResult | None:
    entry = _region_cache.get(region_code)
    if not entry:
        return None
    expires_at, value = entry
    if now >= expires_at:
        _region_cache.pop(region_code, None)
        return None
    return value


async def _generate(models: ModelProvider, region: WorldRegion) -> WorldBriefingResult | None:
    try:
        grounded = await models.grounded(
            _build_user_prompt(region),
            system=WORLD_BRIEFING_SYSTEM_PROMPT,
            temperature=0.6,
        )
    except Exception as exc:
        logger.warn("world_briefing: grounded generation failed", {
            "region": region.country_code,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return None

    narrative, chat_seed = _parse_output(grounded.text)
    narrative = narrative[:NARRATIVE_MAX_CHARS]
    if not narrative:
        logger.info("world_briefing: empty narrative from grounded call", {
            "region": region.country_code,
        })
        return None

    sources = _sources_payload(grounded.sources)
    item_texts = _split_items(narrative)
    citations = _assign_citations(item_texts, grounded.supports, len(sources))
    items = [{"text": t, "citation": c} for t, c in zip(item_texts, citations)]

    result = WorldBriefingResult(
        narrative=narrative,
        chat_seed_message=chat_seed[:CHAT_SEED_MAX_CHARS],
        region_code=region.country_code,
        sources=sources,
        items=items,
    )
    logger.info("world_briefing: generated", {
        "region": region.country_code,
        "narrative_len": len(narrative),
        "items": len(items),
        "sources": len(sources),
        "cited_items": sum(1 for c in citations if c is not None),
    })
    return result


async def generate_world(
    user_id: str,
    *,
    timezone: str,
    force: bool = False,
    models: ModelProvider | None = None,
) -> WorldBriefingResult | None:
    """Produce (or serve cached) the on-demand world snapshot for this user.

    ``force`` is the refresh icon: it bypasses the warm region cache to regenerate, but
    is coalesced back to the cache when the user is still inside their refresh cooldown,
    so the icon can never spam fresh grounded calls. Returns ``None`` on any failure (the
    handler then answers ``{"briefing": null}`` and the screen shows its empty state).
    """
    models = models or get_model_provider()
    region = resolve_region(timezone)
    now = time.monotonic()

    if not force:
        cached = _region_cache_get(region.country_code, now)
        if cached is not None:
            return cached
    else:
        last = _user_refresh_at.get(user_id)
        cooling_down = last is not None and (now - last) < settings.WORLD_BRIEFING_REFRESH_COOLDOWN_SECONDS
        if cooling_down:
            cached = _region_cache_get(region.country_code, now)
            if cached is not None:
                logger.info("world_briefing: force-refresh coalesced to cache (cooldown)", {
                    "user_id": user_id, "region": region.country_code,
                })
                return cached

    result = await _generate(models, region)
    if result is None:
        return None

    _region_cache[region.country_code] = (
        now + settings.WORLD_BRIEFING_CACHE_TTL_SECONDS,
        result,
    )
    _user_refresh_at[user_id] = now
    return result
