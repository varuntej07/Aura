"""
POST /chat: text-based conversation via Claude with SSE streaming.

SSE event format (each line: "data: <json>\n\n"):
  {"type": "text_delta",      "delta": str}
  {"type": "tool_thinking",   "message": str}
  {"type": "clarification_ui","clarification_id": str, "question": str,
                               "options": list[str], "multi_select": bool}
  {"type": "done",            "metadata": {"tool_names": list, "reminder"?: dict,
                                            "awaiting_clarification"?: bool}}
  {"type": "error",           "message": str}
Terminated by: "data: [DONE]\n\n"
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncGenerator
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.responses import StreamingResponse

from ..agents.system_prompts import get_system_prompt
from ..config.settings import settings
from ..lib.logger import logger
from ..lib.query_logger import log_query
from ..services.claude_client import ClaudeClient
from ..services.request_auth import resolve_user_id
from ..services.tool_executor import ToolExecutor
from ..services.user_aura_extractor import extract_and_update_user_aura

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


async def _get_user_local_datetime(uid: str) -> str:
    """Return 'Monday, 3 May 2026 14:32 IST' in the user's timezone, falling back to UTC."""
    from ..services.firebase import admin_firestore

    def _fetch() -> str | None:
        try:
            snap = admin_firestore().collection("users").document(uid).get()
            d = snap.to_dict()
            return d.get("timezone") if d else None
        except Exception:
            return None

    tz_str = await asyncio.to_thread(_fetch)
    try:
        tz = ZoneInfo(tz_str) if tz_str else timezone.utc
    except (ZoneInfoNotFoundError, Exception):
        tz = timezone.utc

    now = datetime.now(tz)
    return now.strftime("%A, %-d %B %Y %H:%M %Z")


def _resolve_user_id(event: dict[str, Any], body: dict[str, Any]) -> str | None:
    try:
        return event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        pass
    uid = body.get("user_id")
    explicit_uid = str(uid) if isinstance(uid, str) and uid else None
    return resolve_user_id(
        event.get("headers"),
        explicit_user_id=explicit_uid if not settings.is_production else None,
    )


def _error_stream(message: str) -> AsyncGenerator[str, None]:
    async def _gen():
        yield f"data: {json.dumps({'type': 'error', 'message': message})}\n\n"
        yield "data: [DONE]\n\n"

    return _gen()


def _sse_error_response(
    message: str,
    *,
    status_code: int,
    headers: dict[str, str],
) -> StreamingResponse:
    return StreamingResponse(
        _error_stream(message),
        media_type="text/event-stream",
        status_code=status_code,
        headers=headers,
    )


def _get_aura_cache_lock(uid: str) -> asyncio.Lock:
    if uid not in _aura_cache_locks:
        _aura_cache_locks[uid] = asyncio.Lock()
    return _aura_cache_locks[uid]


async def _fetch_cached_aura_data(
    uid: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = datetime.now(timezone.utc)

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
            from ..services.firebase import admin_firestore

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


def _build_injected_system_prompt_suffix(
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

    # Top 3 interest areas ranked by message frequency -- gives Buddy domain context.
    interest_freq: dict[str, int] = profile.get("deep_interest_frequencies", {})
    if interest_freq:
        top_interests = [
            k for k, _ in sorted(interest_freq.items(), key=lambda x: x[1], reverse=True)[:3]
        ]
        sections.append(f"<interests>{', '.join(top_interests)}</interests>")
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


async def handle_chat_stream(event: dict[str, Any]) -> StreamingResponse:
    _sse_headers = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
    }

    try:
        body: dict[str, Any] = json.loads(event.get("body") or "{}")
    except json.JSONDecodeError:
        return _sse_error_response("Invalid JSON body", status_code=400, headers=_sse_headers)

    user_id = _resolve_user_id(event, body)
    if not user_id:
        logger.warn("Chat: rejected — missing user_id")
        return _sse_error_response(
            "Unauthorized: user_id required",
            status_code=401,
            headers=_sse_headers,
        )

    message = str(body.get("message", "")).strip()
    if not message:
        logger.warn("Chat: rejected — empty message", {"user_id": user_id})
        return _sse_error_response("message is required", status_code=400, headers=_sse_headers)
    if len(message) > 8_000:
        logger.warn(
            "Chat: rejected — message too long",
            {"user_id": user_id, "message_len": len(message)},
        )
        return _sse_error_response(
            "message must be 8 000 characters or fewer",
            status_code=400,
            headers=_sse_headers,
        )

    raw_session_id = body.get("session_id")
    session_id = (
        raw_session_id.strip()
        if isinstance(raw_session_id, str) and raw_session_id.strip()
        else None
    )

    raw_history: list[Any] = (body.get("history") or [])[-settings.CHAT_HISTORY_WINDOW * 2 :]
    history = [
        {"role": str(h.get("role", "")), "content": str(h.get("content", ""))}
        for h in raw_history
        if isinstance(h, dict) and h.get("role") in ("user", "assistant") and h.get("content")
    ][: settings.CHAT_HISTORY_WINDOW]

    client_message_id: str | None = body.get("client_message_id") or None
    agent_id: str | None = body.get("agent_id") or None

    prev_buddy_response: str | None = next(
        (h["content"] for h in reversed(history) if h["role"] == "assistant"),
        None,
    )

    # Build system prompt: datetime + learned Aura behavioral hints + agent persona + default prompt
    datetime_line = f"Current date and time: {await _get_user_local_datetime(user_id)}"
    aura_profile, accepted_hints = await _fetch_cached_aura_data(user_id)
    aura_suffix = _build_injected_system_prompt_suffix(aura_profile, accepted_hints, user_id)
    agent_prompt = get_system_prompt(agent_id) if agent_id else None
    effective_system_prompt = (
        f"{datetime_line}\n\n{agent_prompt}\n\n---\n\n{settings.BUDDY_CHAT_SYSTEM_PROMPT}"
        if agent_prompt
        else f"{datetime_line}\n\n{settings.BUDDY_CHAT_SYSTEM_PROMPT}"
    )
    if aura_suffix:
        effective_system_prompt += aura_suffix

    await log_query(
        user_id,
        "chat",
        message,
        session_id=session_id,
        client_message_id=client_message_id,
    )
    asyncio.create_task(
        extract_and_update_user_aura(user_id, message, session_id, prev_buddy_response)
    )

    logger.info(
        "Chat: stream request received",
        {
            "user_id": user_id,
            "session_id": session_id,
            "agent_id": agent_id,
            "message_len": len(message),
            "history_turns": len(history),
        },
    )

    start_ts = time.monotonic()

    async def _generate() -> AsyncGenerator[str, None]:
        try:
            tool_executor = ToolExecutor(user_id)
            claude = ClaudeClient(tool_executor)
            async for sse_event in claude.send_text_turn_stream(
                system_prompt=effective_system_prompt,
                user_text=message,
                history=history,
                is_agent=bool(agent_id),
            ):
                yield f"data: {json.dumps(sse_event)}\n\n"
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            logger.info(
                "Chat: stream complete",
                {
                    "user_id": user_id,
                    "duration_ms": duration_ms,
                },
            )
        except Exception as exc:
            duration_ms = int((time.monotonic() - start_ts) * 1000)
            logger.exception(
                "Chat: stream failed",
                {
                    "user_id": user_id,
                    "duration_ms": duration_ms,
                    "error": str(exc),
                    "error_type": type(exc).__name__,
                },
            )
            yield f"data: {json.dumps({'type': 'error', 'message': 'Internal server error'})}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_sse_headers)
