"""
SuggestionPillsAgent — generates the main Buddy chat suggestion pills.

Runs in two places, both writing to Firestore at
    agent_suggestion_pills/{user_id}  →  { "buddy": [...], "buddy_generated_at": ... }
  - the daily notification pipeline (orchestrator.py, after the daily plan is written)
  - the on-demand refresh endpoint (fired when the user leaves the app after a text or
    voice session)

Pills are grounded in the user's UserAura interest subjects (consent-gated, passed in
already) plus their recent chat queries. Each pill is 3-6 words, written in the user's
own first-person voice so tapping one drops a natural message into the input box. On any
failure generation is skipped silently; the Flutter app falls back to hardcoded defaults.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

from ...lib.logger import logger
from ...prompts import (
    HOME_DECK_SYSTEM_PROMPT,
    SUGGESTION_PILLS_SYSTEM_PROMPT,
    home_deck_user_prompt,
    suggestion_pills_user_prompt,
)
from ...services.firebase import admin_firestore
from ...services.model_provider import ModelProvider
from ...services.reactive.intent_store import list_open_subjects
from .home_deck import (
    STATIC_BRIEFING_CARD,
    STATIC_CONNECT_CARD,
    HomeDeckBundle,
    clean_pills,
    sanitize_cards,
)

# The model writes two cards: the ritual and the reminder. The briefing and
# connectors slots are static and appended below.
DECK_CARD_COUNT = 2

# How many phrases each of those two cards flashes.
DECK_SUGGESTIONS_PER_CARD = 4

# Pending intents are the warmest grounding available for the reminder phrases:
# they are already the things a caring friend would check back on, written from
# the user's own speech rather than inferred here.
_OPEN_THREAD_LIMIT = 8

# Buddy is general-purpose and its pills land directly in the user's input box, so they
# must read as a real message the user sends — not a terse search fragment. This system
# prompt pins the user's first-person voice and the no-question-mark rule.

class SuggestionPillsAgent:
    def __init__(self, models: ModelProvider) -> None:
        self._models = models

    async def generate_buddy_pills(
        self,
        user_id: str,
        recent_queries: list[dict],
        interest_subjects: list[str] | None = None,
    ) -> list[str]:
        """Generate the main Buddy chat pills and write them to Firestore.

        Grounded in the user's interest subjects (already consent-gated) plus their
        recent queries. Returns the pills (empty list on failure). Used by both the
        daily run and the on-demand refresh endpoint.
        """
        queries = [
            str(query.get("text", "")).strip()
            for query in recent_queries[:10]
            if str(query.get("text", "")).strip()
        ]
        interests = interest_subjects or []
        open_threads = await _fetch_open_threads(user_id)

        # One call covers both home surfaces. The deck and the pills need identical
        # grounding, so generating them separately would double this subsystem's
        # call count at every user count for no extra signal.
        pills: list[str] = []
        cards: list[dict] = []
        try:
            bundle: HomeDeckBundle = await self._models.balanced(
                home_deck_user_prompt(
                    recent_queries=queries,
                    interest_subjects=interests,
                    open_threads=open_threads,
                    suggestions_per_card=DECK_SUGGESTIONS_PER_CARD,
                ),
                system=HOME_DECK_SYSTEM_PROMPT,
                response_model=HomeDeckBundle,
                # Four cards that all sound alike is the failure mode here, so this
                # sits above the pills-only temperature but below improvisation.
                temperature=0.4,
            )
            pills = clean_pills(bundle.pills)
            cards = _assemble_deck(user_id, sanitize_cards(bundle.cards))
        except Exception as exc:
            logger.warn("suggestion_pills: deck generation failed", {
                "user_id": user_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })

        if not pills:
            # The pills ship to app versions that know nothing about the deck, so a
            # deck-shaped failure must never cost them. Fall back to the original
            # pills-only call, unchanged.
            raw: str = await self._models.balanced(
                suggestion_pills_user_prompt(
                    recent_queries=queries,
                    interest_subjects=interests,
                ),
                system=SUGGESTION_PILLS_SYSTEM_PROMPT,
                temperature=0.3,
            )
            pills = _parse_pills(raw)

        if pills or cards:
            await _write_buddy_pills(user_id, pills, cards)
        return pills


async def _fetch_open_threads(user_id: str) -> list[str]:
    """The user's pending intents, as plain phrases for the reminder card.

    Already written from the user's own speech by intent_sense, so nothing here
    inspects or matches on what they said. Returns [] on any failure, which the
    prompt renders as "none" rather than as a broken section.
    """
    try:
        intents = await list_open_subjects(user_id, limit=_OPEN_THREAD_LIMIT)
    except Exception as exc:
        logger.warn("suggestion_pills: open thread fetch failed", {
            "user_id": user_id,
            "error": str(exc),
            "error_type": type(exc).__name__,
        })
        return []
    threads: list[str] = []
    for intent in intents:
        text = (getattr(intent, "title", "") or getattr(intent, "question", "") or "").strip()
        if text:
            threads.append(text)
    return threads


def _assemble_deck(user_id: str, cards: list[dict]) -> list[dict]:
    """The fixed hand: the generated ritual and reminder, then the two static slots.

    Returns [] when either generated card is missing, which stops the caller from
    writing `home_deck` at all and leaves the last good deck on the phone. A hand
    silently missing its ritual card is the thing that made this screen useless in
    the first place, so it is logged loudly rather than shipped half-empty.
    """
    ritual = next((card for card in cards if card.get("kind") == "ritual"), None)
    remind = next((card for card in cards if card.get("kind") == "remind"), None)
    if ritual is None or remind is None:
        logger.warn("suggestion_pills: incomplete deck, keeping previous", {
            "user_id": user_id,
            "has_ritual": ritual is not None,
            "has_remind": remind is not None,
            "kinds": [card.get("kind") for card in cards],
        })
        return []
    # Static slots are appended server-side so installs that predate the fixed
    # hand still read four cards out of this document.
    return [ritual, remind, STATIC_BRIEFING_CARD, STATIC_CONNECT_CARD]


def _parse_pills(raw: str) -> list[str]:
    """Parse a JSON array of strings from the LLM response. Returns empty list on failure."""
    try:
        cleaned = raw.strip()
        # Strip markdown fences if present
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        pills = json.loads(cleaned)
        if isinstance(pills, list):
            valid = [
                p.strip()
                for p in pills
                if isinstance(p, str) and p.strip() and len(p.strip().split()) <= 6
            ]
            return valid[:5]
    except Exception as exc:
        logger.warn("suggestion_pills: failed to parse LLM response", {
            "error": str(exc),
            "raw_preview": raw[:100],
        })
    return []


async def _write_buddy_pills(user_id: str, pills: list[str], cards: list[dict]) -> None:
    """Write the buddy pill set, the home deck, and their freshness stamps.

    Uses merge so it never clobbers any other keys the doc may still hold for older
    app clients reading the legacy per-agent sets. `buddy` keeps its exact shipped
    shape (a list of strings) and the deck lands beside it under its own key, so an
    installed app that has never heard of cards is unaffected. Each key is written
    only when it has content: a deck that failed to generate must not blank the one
    already on the phone."""
    def _write() -> None:
        db = admin_firestore()
        now_iso = datetime.now(UTC).isoformat()
        payload: dict = {"updated_at": now_iso}
        if pills:
            payload["buddy"] = pills
            payload["buddy_generated_at"] = now_iso
        if cards:
            payload["home_deck"] = cards
            payload["home_deck_generated_at"] = now_iso
        db.collection("agent_suggestion_pills").document(user_id).set(
            payload,
            merge=True,
        )

    try:
        await asyncio.to_thread(_write)
        logger.info("suggestion_pills: buddy pills written", {
            "user_id": user_id,
            "count": len(pills),
        })
    except Exception as exc:
        logger.exception("suggestion_pills: failed to write buddy pills", {
            "user_id": user_id,
            "error": str(exc),
        })
