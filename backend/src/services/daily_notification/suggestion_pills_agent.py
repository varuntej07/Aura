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
from ...services.firebase import admin_firestore
from ...services.model_provider import ModelProvider

# Buddy is general-purpose and its pills land directly in the user's input box, so they
# must read as a real message the user sends — not a terse search fragment. This system
# prompt pins the user's first-person voice and the no-question-mark rule.
_BUDDY_SYSTEM_PROMPT = """You write tappable chat starters for one person to send to Buddy, their personal AI companion.
Every starter is a line the USER types TO Buddy, in the user's own first-person voice, never a line Buddy says to the user.
Hard rules:
- Sound like a real person texting a close friend: natural, casual, and complete.
- Each starter covers exactly ONE topic. Never glue two different subjects into one line (never "Trainium for my app widget").
- First-person voice only. Start it the way you'd actually text Buddy ("help me...", "i'm stuck on...", "remind me to...", "let's..."). Never a bare noun phrase or a label.
- NEVER end with a question mark, and never write a terse search-query fragment. Write "what's going on in the transfer window", not "FIFA updates today?".
- 3 to 6 words each. No emojis, no quotation marks inside a pill, no markdown.
Return ONLY a JSON array of strings, nothing else."""


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
        prompt = _build_buddy_prompt(recent_queries, interest_subjects)
        # Pills run off the hot path (generated on app-background and by the daily
        # job), so latency and cost barely matter here. Use the mid tier (Haiku) at a
        # low temperature for tighter instruction-following: it merges topics and slips
        # out of the user's first-person voice far less than the cheap tier did.
        raw: str = await self._models.balanced(
            prompt, system=_BUDDY_SYSTEM_PROMPT, temperature=0.3
        )
        pills = _parse_pills(raw)
        if pills:
            await _write_buddy_pills(user_id, pills)
        return pills


def _build_buddy_prompt(
    recent_queries: list[dict],
    interest_subjects: list[str] | None = None,
) -> str:
    lines = [
        "Agent: Buddy — the user's personal AI companion for anything: reminders, "
        "plans, decisions, questions, and picking up wherever they left off.",
        "",
    ]

    if interest_subjects:
        lines.append("Things this user cares about (their interests):")
        for subject in interest_subjects[:5]:
            lines.append(f"  - {subject}")
        lines.append("")

    relevant_queries = [
        q.get("text", "").strip()
        for q in recent_queries[:10]
        if q.get("text", "").strip()
    ]
    if relevant_queries:
        lines.append("User's recent queries (use for context, not literally):")
        for q in relevant_queries[:5]:
            lines.append(f"  - {q}")
        lines.append("")

    # Buddy is general-purpose; ground the pills in the user's own world so a
    # returning user sees threads worth picking back up, not generic prompts.
    # CRITICAL: tapping a pill drops the text into the user's input box, so each
    # pill must read as something the USER types TO Buddy — never as a question
    # Buddy asks the user (that inverts the meaning and reads as nonsense).
    lines.append(
        "Write exactly 3 chat starters this user would tap to text Buddy right "
        "now, each in their own first-person voice, 3-6 words each, each about "
        "ONE topic only.\n"
        "WHAT TO DRAW FROM (in priority order):\n"
        "  1. Their interests above. Anchor the FIRST TWO starters on the "
        "specific subjects they care about, each continuing a real project, "
        "goal, or curiosity. One subject per starter, never two glued into one "
        "line.\n"
        "  2. Their recent queries, only to sharpen what they're actively into. "
        "IGNORE anything one-off or already done: errands and logistics (\"going "
        "to the bank\", \"pick up groceries\"), and time-bound events that have "
        "likely already happened (a meeting, an appointment, a deadline). A "
        "wrapped-up event is not a live thread, so never resurface it.\n"
        "  3. Make the THIRD starter a fresh angle: a natural next step on one "
        "of the real topics above, or a warm broadly-useful starter. It must "
        "still be concrete and about something specific. Never a content-free "
        "filler line.\n"
        "  If no interests or queries are given, write all 3 as warm, broadly "
        "useful starters in the user's voice.\n"
        "PHRASING, this is what makes or breaks it:\n"
        "  - Each starter is the USER talking TO Buddy, so it must read like a "
        "message they'd actually send, not a search-box query or a label.\n"
        "  - NO question marks. NO terse fragments. NO bare noun phrases. Say it "
        "the way you'd text a close friend.\n"
        "GOOD (the user's voice): \"help me prep for my interview\", \"hold me "
        "to the gym today\", \"i'm stuck on the React bug again\", \"recommend "
        "me a sci-fi book\".\n"
        "BAD, never produce these:\n"
        "  - \"Trainium for my app widget\" (two topics glued together, and a "
        "bare noun phrase).\n"
        "  - \"Help me think this through\", \"let's figure things out\" "
        "(content-free filler that is about nothing).\n"
        "  - \"FIFA updates for today?\", \"Recommend a new book?\", \"Player "
        "stats?\" (search fragments and question marks).\n"
        "  - \"What's on your plate today?\", \"How can I help?\" (Buddy talking "
        "instead of the user)."
    )
    return "\n".join(lines)


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
