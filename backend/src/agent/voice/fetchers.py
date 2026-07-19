"""Firestore reads that seed a voice session's prompt context.

Every fetcher runs its blocking Firestore call on a worker thread and returns a
fully-defaulted value, so a missing document or a read error degrades to "no
context" instead of failing the session.
"""

from __future__ import annotations

import asyncio

from google.cloud.firestore_v1.base_query import FieldFilter

from ...lib.logger import logger
from ...services.firebase import admin_firestore
from ...services.memory import graph_fields as GF
from ...services.memory.salience import normalized_graph_salience
from ...services.user_aura_schema import interest_prompt_lines
from .text_sanitizer import sanitize_for_speech

# Memory keys that are already carried by a dedicated, live prompt slot and so must
# NOT be echoed back through the memory digest. A stored "timezone" memory (written by
# a past store_memory call) goes stale and contradicts the live {timezone} slot, which
# is sourced fresh from the user profile every session. Compared case-insensitively.
_PROFILE_OWNED_MEMORY_KEYS = {"timezone"}

GRAPH_DIGEST_LIMIT = 8


async def fetch_user_profile(user_id: str) -> dict[str, str]:
    """Return {name, timezone} from users/{uid}. Defaults fill missing fields."""
    def _read() -> dict[str, str]:
        doc = admin_firestore().collection("users").document(user_id).get()
        data = doc.to_dict() or {}
        return {
            "name": (data.get("display_name") or data.get("name") or "").strip() or "there",
            "timezone": (data.get("timezone") or "UTC").strip() or "UTC",
        }
    return await asyncio.to_thread(_read)


async def fetch_memory_summary(user_id: str) -> str:
    """Top 5 recent rows from users/{uid}/memories, formatted as bullet lines."""
    def _read() -> str:
        coll = admin_firestore().collection("users").document(user_id).collection("memories")
        try:
            docs = list(coll.order_by("updated_at", direction="DESCENDING").limit(5).stream())
        except Exception:
            docs = list(coll.limit(5).stream())
        if not docs:
            return ""
        lines: list[str] = []
        for d in docs:
            row = d.to_dict() or {}
            key = str(row.get("key", "")).strip()
            # Strip any markdown stored in the value so it reads cleanly when this digest
            # is injected into the voice prompt (and is mirrored by the TTS sanitizer).
            value = sanitize_for_speech(str(row.get("value", "")).strip())
            if key and value and key.casefold() not in _PROFILE_OWNED_MEMORY_KEYS:
                lines.append(f"- {key}: {value}")
        return "\n".join(lines)
    return await asyncio.to_thread(_read)


async def fetch_graph_digest(user_id: str) -> str:
    """Read and rank a compact graph digest with no embedding call. Fail-open."""
    try:
        def _read() -> str:
            nodes = (
                admin_firestore()
                .collection(GF.PARENT_COLLECTION)
                .document(user_id)
                .collection(GF.NODE_SUBCOLLECTION)
            )
            query = (
                nodes.where(filter=FieldFilter(
                    GF.STATUS,
                    "in",
                    [GF.NODE_STATUS_ACTIVE, GF.NODE_STATUS_DORMANT],
                ))
                .order_by(GF.WEIGHT, direction="DESCENDING")
                .limit(GRAPH_DIGEST_LIMIT)
            )
            ranked: list[tuple[float, str]] = []
            for snap in query.stream():
                data = snap.to_dict() or {}
                salience = normalized_graph_salience(data)
                if salience <= 0.0:
                    continue
                display = sanitize_for_speech(
                    str(data.get(GF.DISPLAY) or data.get(GF.ENTITY) or "").strip()
                )
                if display:
                    ranked.append((salience, display))
            ranked.sort(key=lambda row: row[0], reverse=True)
            return "\n".join(f"- {display}" for _, display in ranked[:GRAPH_DIGEST_LIMIT])

        return await asyncio.to_thread(_read)
    except Exception as exc:
        logger.warn(
            "VoiceSession: graph digest failed open",
            {
                "user_id": user_id,
                "error": str(exc),
                "error_type": type(exc).__name__,
            },
        )
        return ""


async def fetch_last_session_summary(user_id: str) -> dict[str, str]:
    """Read users/{uid}/voice_session_state/latest. Returns {summary, last_session_at} or empty."""
    def _read() -> dict[str, str]:
        doc = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_session_state").document("latest")
            .get()
        )
        data = doc.to_dict() or {}
        return {
            # Schema-v2 carries compact structured memory separately from the friendly
            # history recap. Legacy docs fall back to the old summary field.
            "summary": sanitize_for_speech(
                str(data.get("memory_context") or data.get("summary", ""))
            ),
            "last_session_at": str(data.get("last_session_at", "")),
        }
    return await asyncio.to_thread(_read)


async def fetch_archive_context(user_id: str) -> dict[str, str]:
    """Read users/{uid}/voice_session_state/archive. Returns {archive_summary} or empty."""
    def _read() -> dict[str, str]:
        doc = (
            admin_firestore()
            .collection("users").document(user_id)
            .collection("voice_session_state").document("archive")
            .get()
        )
        data = doc.to_dict() or {}
        return {"archive_summary": str(data.get("archive_summary", ""))}
    return await asyncio.to_thread(_read)


async def fetch_user_aura_profile(user_id: str) -> dict[str, str]:
    """Read UserAura/{uid} once and return both the prompt block and raw voice signals.

    Returns {summary, dominant_tone, dominant_emotion}. `summary` is the
    prompt-ready behavioral block; `dominant_tone` is the user's communication style;
    `dominant_emotion` is the argmax of the accumulated `emotional_signals`
    frequency map (no single field stores it).
    All default to "" when absent so a profile-less user changes nothing downstream.
    """
    def _read() -> dict[str, str]:
        doc = admin_firestore().collection("UserAura").document(user_id).get()
        data = doc.to_dict() or {}
        if not data:
            return {"summary": "", "dominant_tone": "", "dominant_emotion": ""}
        lines: list[str] = []

        tone = data.get("dominant_tone", "")
        depth = data.get("response_depth_preference", "")
        style_parts = [p for p in [tone, depth] if p]
        if style_parts:
            lines.append(f"Communication style: {', '.join(style_parts)}")

        interest_lines = interest_prompt_lines(data)
        if interest_lines:
            lines.append(f"Interests: {'; '.join(interest_lines)}")

        facts: list = data.get("explicit_facts", [])[:5]
        if facts:
            lines.append(f"Facts they've shared: {'; '.join(facts)}")

        goals: list = data.get("inferred_goals", [])[-3:]
        if goals:
            lines.append(f"Current goals: {'; '.join(goals)}")

        prefer: list = data.get("response_style_prefer", [])[-2:]
        avoid: list = data.get("response_style_avoid", [])[-2:]
        if prefer:
            lines.append(f"What's worked well: {'; '.join(prefer)}")
        if avoid:
            lines.append(f"What to avoid: {'; '.join(avoid)}")

        emotional_signals: dict = data.get("emotional_signals", {}) or {}
        dominant_emotion = (
            max(emotional_signals, key=lambda k: emotional_signals[k])
            if emotional_signals else ""
        )

        return {
            "summary": "\n".join(f"- {line}" for line in lines),
            "dominant_tone": str(tone or ""),
            "dominant_emotion": str(dominant_emotion or ""),
        }

    return await asyncio.to_thread(_read)
