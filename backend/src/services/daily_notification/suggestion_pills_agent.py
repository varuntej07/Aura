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
from ...prompts import SUGGESTION_PILLS_SYSTEM_PROMPT, suggestion_pills_user_prompt
from ...services.firebase import admin_firestore
from ...services.model_provider import ModelProvider

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
        prompt = suggestion_pills_user_prompt(
            recent_queries=[
                str(query.get("text", "")).strip()
                for query in recent_queries[:10]
                if str(query.get("text", "")).strip()
            ],
            interest_subjects=interest_subjects or [],
        )
        # Pills run off the hot path (generated on app-background and by the daily
        # job), so latency and cost barely matter here. Use the mid tier (Haiku) at a
        # low temperature for tighter instruction-following: it merges topics and slips
        # out of the user's first-person voice far less than the cheap tier did.
        raw: str = await self._models.balanced(
            prompt, system=SUGGESTION_PILLS_SYSTEM_PROMPT, temperature=0.3
        )
        pills = _parse_pills(raw)
        if pills:
            await _write_buddy_pills(user_id, pills)
        return pills


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


async def _write_buddy_pills(user_id: str, pills: list[str]) -> None:
    """Write the buddy pill set + freshness stamp to agent_suggestion_pills/{uid}.

    Uses merge so it never clobbers any other keys the doc may still hold for older
    app clients reading the legacy per-agent sets."""
    def _write() -> None:
        db = admin_firestore()
        now_iso = datetime.now(UTC).isoformat()
        db.collection("agent_suggestion_pills").document(user_id).set(
            {"buddy": pills, "buddy_generated_at": now_iso, "updated_at": now_iso},
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
