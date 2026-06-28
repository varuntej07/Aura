"""Shared system-prompt construction for the chat turn.

Extracted from ``handlers/chat.py`` so the live SSE handler AND the durable
completion path build the EXACT same Anthropic system prompt (same aura profile,
same prompt-cache breakpoints, same query-relevant memory block). If these two
ever drift, a backgrounded turn would be answered with a different prompt than the
foreground one, which is exactly the kind of silent inconsistency this app guards
against.

These helpers only read services (settings, Firestore, the aura schema, memory
retrieval); they hold no handler/request state, so they live in the service layer.
``handlers/chat.py`` re-exports them under their old private names for the existing
tests.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ...config.settings import settings
from ...lib.logger import logger
from ..memory.retrieval import (
    render_relevant_memory_block,
    retrieve_relevant_memory,
)
from ..user_aura_schema import active_category_slugs, interest_prompt_lines

# Process-global aura cache (per-uid profile + accepted hints). Shared across the
# live handler and the completion path; either may populate it.
_aura_cache: dict[str, dict[str, Any]] = {}
_aura_cache_locks: dict[str, asyncio.Lock] = {}
_AURA_CACHE_TTL_SECONDS = 600

# Maps Gemini-extracted tone values to natural language descriptions for the system prompt.
# Descriptive framing is more effective than imperative ("MUST be brief") per Anthropic guidance.
_TONE_DESCRIPTIONS: dict[str, str] = {
    "casual": "casual and conversational",
    "terse": "terse and to the point",
    "verbose": "detailed and thorough",
    "formal": "formal and structured",
    "playful": "light and playful",
}

# Maps depth preference signals to instructional sentences injected into the system prompt.
_DEPTH_INSTRUCTIONS: dict[str, str] = {
    "wants_brief": "Keep responses concise. This user consistently signals preference for shorter answers.",
    "wants_detailed": "This user appreciates thorough explanations. Do not cut corners.",
    "wants_step_by_step": "Break things down step by step. This user follows structured explanations well.",
    "wants_examples": "Include concrete examples. This user learns better from them than from abstract descriptions.",
    "wants_opinion": "This user values direct recommendations, not just neutral facts.",
}

# Defensive cap on the injected "why you reached out" note (~100 words). The
# producers already keep it short; this just guards a malformed client payload.
NOTIFICATION_REASON_MAX_CHARS = 600


async def get_user_local_datetime(uid: str) -> str:
    """Return 'Monday, 3 May 2026 14:32 IST' in the user's timezone, falling back to UTC."""
    from ..firebase import admin_firestore

    def _fetch() -> str | None:
        try:
            snap = admin_firestore().collection("users").document(uid).get()
            d = snap.to_dict()
            return d.get("timezone") if d else None
        except Exception:
            return None

    tz_str = await asyncio.to_thread(_fetch)
    try:
        tz = ZoneInfo(tz_str) if tz_str else UTC
    except (ZoneInfoNotFoundError, Exception):
        tz = UTC

    now = datetime.now(tz)
    return now.strftime(f"%A, {now.day} %B %Y %H:%M %Z")


def _get_aura_cache_lock(uid: str) -> asyncio.Lock:
    if uid not in _aura_cache_locks:
        _aura_cache_locks[uid] = asyncio.Lock()
    return _aura_cache_locks[uid]


async def aura_consent_revoked(uid: str) -> bool:
    """True only when the user has EXPLICITLY withdrawn Aura consent — i.e.
    users/{uid}.aura_consent_granted is present and False. Absent or True reads as
    not-revoked, so this never changes behavior for accounts that predate the
    in-app memory toggle (deploy-safe: only an explicit in-app revoke stops
    personalization). Fail-open on a read error: a transient Firestore failure
    must not silently drop a consented user's personalization, and the next
    successful read applies a real revoke within a turn.
    """
    from ..firebase import admin_firestore

    def _fetch() -> bool:
        try:
            snap = admin_firestore().collection("users").document(uid).get()
            if not snap.exists:
                return False
            return (snap.to_dict() or {}).get("aura_consent_granted", None) is False
        except Exception:
            return False

    return await asyncio.to_thread(_fetch)


async def fetch_cached_aura_data(
    uid: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = datetime.now(UTC)

    # GDPR withdrawal: if the user has explicitly turned Aura memory off, do not
    # read or inject their stored profile (the writer is already consent-gated in
    # user_aura_extractor). This makes the in-app revoke stop personalization
    # within a turn instead of letting the frozen profile keep shaping chat.
    if await aura_consent_revoked(uid):
        logger.info("Chat: Aura memory revoked, skipping profile personalization", {"user_id": uid})
        return {}, []

    cached = _aura_cache.get(uid)
    if cached and (now - cached["fetched_at"]).total_seconds() < _AURA_CACHE_TTL_SECONDS:
        ttl_remaining = int(_AURA_CACHE_TTL_SECONDS - (now - cached["fetched_at"]).total_seconds())
        logger.info("Chat: Aura cache hit", {"user_id": uid, "ttl_remaining_s": ttl_remaining})
        return cached["profile"], cached["accepted_hints"]

    # Acquire a per-uid lock before hitting Firestore. If multiple requests arrive
    # simultaneously for a cold cache entry, only one will fetch -- the rest wait
    # and then hit the cache on the double-check below (standard stampede prevention).
    lock = _get_aura_cache_lock(uid)
    async with lock:
        cached = _aura_cache.get(uid)
        if cached and (now - cached["fetched_at"]).total_seconds() < _AURA_CACHE_TTL_SECONDS:
            logger.info("Chat: Aura cache hit after lock (populated by concurrent request)", {
                "user_id": uid,
            })
            return cached["profile"], cached["accepted_hints"]

        try:
            from ..firebase import admin_firestore

            def _fetch() -> tuple[dict[str, Any], list[dict[str, Any]]]:
                db = admin_firestore()
                profile_snap = db.collection("UserAura").document(uid).get()
                profile = profile_snap.to_dict() or {}
                hints_query = (
                    db.collection("UserSignals")
                    .document(uid)
                    .collection("accepted_hints")
                    .order_by("timestamp", direction="DESCENDING")
                    .limit(5)
                )
                accepted_hints = [doc.to_dict() for doc in hints_query.stream() if doc.to_dict()]
                return profile, accepted_hints

            profile, accepted_hints = await asyncio.to_thread(_fetch)
            _aura_cache[uid] = {
                "profile": profile,
                "accepted_hints": accepted_hints,
                "fetched_at": now,
            }
            logger.info("Chat: Aura cache populated from Firestore", {
                "user_id": uid,
                "profile_fields": len(profile),
                "accepted_hints_count": len(accepted_hints),
                "has_tone": "dominant_tone" in profile,
                "has_depth_pref": "response_depth_preference" in profile,
                "explicit_facts_count": len(profile.get("explicit_facts", [])),
                "inferred_goals_count": len(profile.get("inferred_goals", [])),
                "deep_interests_count": len(profile.get("deep_interest_frequencies", {})),
            })
            return profile, accepted_hints

        except Exception as exc:
            logger.warn("Chat: Aura Firestore fetch failed, using empty state", {
                "user_id": uid,
                "error": str(exc),
                "error_type": type(exc).__name__,
            })
            return {}, []


def build_injected_system_prompt_suffix(
    profile: dict[str, Any],
    accepted_hints: list[dict[str, Any]],
    uid: str,
) -> str:
    """
    Build an XML-structured suffix appended to Buddy's system prompt.

    Uses XML tags per Anthropic's prompt engineering guidance -- they reduce ambiguity
    and help Claude distinguish injected context from core instructions. Each section
    is only included when the underlying data is non-empty so the prompt stays lean.
    """
    sections: list[str] = []
    injected_fields: list[str] = []

    # Communication style -- tone + depth preference derived from accumulated signals.
    style_parts: list[str] = []
    dominant_tone: str | None = profile.get("dominant_tone")
    depth_pref: str | None = profile.get("response_depth_preference")
    if dominant_tone and dominant_tone in _TONE_DESCRIPTIONS:
        style_parts.append(f"Tone: {_TONE_DESCRIPTIONS[dominant_tone]}")
    if depth_pref and depth_pref in _DEPTH_INSTRUCTIONS:
        style_parts.append(_DEPTH_INSTRUCTIONS[depth_pref])
    if style_parts:
        sections.append("<communication_style>\n" + "\n".join(style_parts) + "\n</communication_style>")
        injected_fields.append("communication_style")

    # Facts the user has explicitly stated (capped at 5 to stay token-efficient).
    facts: list[str] = profile.get("explicit_facts", [])[:5]
    if facts:
        sections.append("<known_facts>\n" + "\n".join(f"- {f}" for f in facts) + "\n</known_facts>")
        injected_fields.append(f"known_facts({len(facts)})")

    # Long-running goals inferred from message history (capped at 3).
    goals: list[str] = profile.get("inferred_goals", [])[:3]
    if goals:
        sections.append("<active_goals>\n" + "\n".join(f"- {g}" for g in goals) + "\n</active_goals>")
        injected_fields.append(f"active_goals({len(goals)})")

    # Top interest areas with the specific subjects inside them (e.g.
    # "politics & governance: KCR") -- gives Buddy domain context plus the named
    # entities that make a reply feel personal. Falls back to legacy free-text
    # interests while a profile rebuilds into the new structure.
    interest_lines = interest_prompt_lines(profile)
    if interest_lines:
        sections.append("<interests>\n" + "\n".join(f"- {line}" for line in interest_lines) + "\n</interests>")
        injected_fields.append("interests")

    # Directive corrections extracted from turns where the user explicitly corrected Buddy.
    if accepted_hints:
        hint_lines = "\n".join(f"- {h['hint']}" for h in accepted_hints if h.get("hint"))
        if hint_lines:
            sections.append(
                "<learned_corrections>\n"
                "Apply these corrections from past interactions with this user:\n"
                + hint_lines
                + "\n</learned_corrections>"
            )
            injected_fields.append(f"learned_corrections({len(accepted_hints)})")

    # Style signals derived from turn scoring -- what worked and what didn't.
    style_avoid: list[str] = profile.get("response_style_avoid", [])
    style_prefer: list[str] = profile.get("response_style_prefer", [])
    guidance_parts: list[str] = []
    if style_avoid:
        guidance_parts.append("Avoid: " + ", ".join(style_avoid))
    if style_prefer:
        guidance_parts.append("Prefer: " + ", ".join(style_prefer))
    if guidance_parts:
        sections.append(
            "<response_guidance>\n" + "\n".join(guidance_parts) + "\n</response_guidance>"
        )
        injected_fields.append("response_guidance")

    if not sections:
        logger.info("Chat: no Aura profile data to inject yet", {"user_id": uid})
        return ""

    suffix = "\n\n<user_profile>\n" + "\n".join(sections) + "\n</user_profile>"
    logger.info("Chat: Aura suffix injected into system prompt", {
        "user_id": uid,
        "injected_fields": injected_fields,
        "suffix_chars": len(suffix),
    })
    return suffix


def build_user_content(
    message: str,
    attachments: list[dict[str, Any]],
) -> str | list[dict[str, Any]]:
    """
    Build the Anthropic user content value.
    Returns a plain string when there are no attachments (common path).
    Returns a content block list when attachments are present.
    """
    if not attachments:
        return message

    blocks: list[dict[str, Any]] = []
    for att in attachments:
        att_type = att.get("type")
        mime = att.get("mime_type", "")
        data = att.get("data", "")
        if att_type == "image":
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mime, "data": data},
            })
        elif att_type == "document":
            blocks.append({
                "type": "document",
                "source": {"type": "base64", "media_type": mime, "data": data},
            })

    if message:
        blocks.append({"type": "text", "text": message})
    return blocks


def build_system_blocks(
    base_system_prompt: str,
    aura_suffix: str,
    local_datetime: str,
    notification_reason: str = "",
) -> list[dict[str, Any]]:
    """
    Build the Anthropic system parameter as a list of TextBlockParams with
    prompt-cache breakpoints.

    Layout (stable → volatile, so the cache prefix is as long as possible):
      Block 1: base prompt                          [cache_control]  — never changes
      Block 2: aura suffix                          [cache_control]  — stable for ~10 min
      Block 3: current datetime                                      — not cached
      Block 4: why-you-reached-out (optional)                        — not cached

    Anthropic evaluates cache breakpoints in tools → system → messages order.
    The list format is required for explicit cache_control placement; a plain
    string only supports automatic (top-level) caching which cannot exclude the
    volatile datetime from the cached prefix.

    ``notification_reason`` is set ONLY on the first turn after a proactive
    notification tap (the client sends it once, then drops it). It is appended
    AFTER the cached prefix so it never pollutes the cache, and it orients Buddy
    on WHY it reached out so it does not disown its own opener when the user
    replies.
    """
    stable_text = base_system_prompt
    blocks: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": stable_text,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        },
    ]
    if aura_suffix:
        blocks.append({
            "type": "text",
            "text": aura_suffix,
            "cache_control": {"type": "ephemeral", "ttl": "1h"},
        })
    blocks.append({
        "type": "text",
        "text": f"Current date and time: {local_datetime}",
    })
    if notification_reason:
        blocks.append({
            "type": "text",
            "text": (
                "WHY YOU REACHED OUT (private context for THIS reply only — you "
                "started this conversation by pinging them; do not quote this note or "
                "mention you have one, just stay oriented):\n"
                f"{notification_reason}"
            ),
        })
    return blocks


async def build_turn_system_blocks(
    uid: str,
    message: str,
    notification_reason: str = "",
) -> list[dict[str, Any]]:
    """Assemble the full system prompt for one chat turn: datetime + aura profile
    suffix + query-relevant long-term memory, in one place so the live handler and
    the durable completion path are guaranteed identical.

    The memory block runs ONLY when the user has a non-empty (consented) profile --
    an empty profile means revoked consent or a brand-new user with no atoms yet, so
    we skip the query embed entirely (GDPR-safe, no wasted call).
    ``retrieve_relevant_memory`` self-bounds and fail-opens, so it can never block or
    break the turn. The block is appended AFTER the cached prefix (same slot as
    notification_reason) so per-turn memory never invalidates the system-prompt cache,
    and is deduped against the static <interests> already shown.
    """
    local_datetime, (aura_profile, accepted_hints) = await asyncio.gather(
        get_user_local_datetime(uid),
        fetch_cached_aura_data(uid),
    )
    aura_suffix = build_injected_system_prompt_suffix(aura_profile, accepted_hints, uid)
    blocks = build_system_blocks(
        settings.BUDDY_CHAT_SYSTEM_PROMPT,
        aura_suffix,
        local_datetime,
        notification_reason,
    )

    if aura_profile:
        relevant_atoms = await retrieve_relevant_memory(
            uid, message, active_slugs=active_category_slugs(aura_profile),
        )
        if relevant_atoms:
            shown_subjects: set[str] = set()
            for line in interest_prompt_lines(aura_profile):
                _, _, subjects = line.partition(": ")
                shown_subjects.update(s.strip() for s in subjects.split(",") if s.strip())
            memory_block = render_relevant_memory_block(relevant_atoms, already_shown=shown_subjects)
            if memory_block:
                blocks.append({"type": "text", "text": memory_block})

    return blocks
