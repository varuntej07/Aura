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
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi.responses import StreamingResponse

from ..config.settings import settings
from ..lib.logger import logger
from ..lib.query_logger import log_query
from ..services.claude_client import ClaudeClient
from ..services.request_auth import resolve_user_id
from ..services.tool_executor import ToolExecutor
from ..services.user_aura_extractor import extract_and_update_user_aura
from ..services.user_aura_schema import interest_prompt_lines

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
        tz = ZoneInfo(tz_str) if tz_str else UTC
    except (ZoneInfoNotFoundError, Exception):
        tz = UTC

    now = datetime.now(tz)
    return now.strftime(f"%A, {now.day} %B %Y %H:%M %Z")


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


def _chat_limit_reached_stream() -> AsyncGenerator[str, None]:
    _payload = json.dumps({
        "type": "chat_limit_reached",
        "message": "That's your free messages for today! Upgrade to keep the conversation going with Buddy.",
    })

    async def _gen():
        yield f"data: {_payload}\n\n"
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


async def _aura_consent_revoked(uid: str) -> bool:
    """True only when the user has EXPLICITLY withdrawn Aura consent — i.e.
    users/{uid}.aura_consent_granted is present and False. Absent or True reads as
    not-revoked, so this never changes behavior for accounts that predate the
    in-app memory toggle (deploy-safe: only an explicit in-app revoke stops
    personalization). Fail-open on a read error: a transient Firestore failure
    must not silently drop a consented user's personalization, and the next
    successful read applies a real revoke within a turn.
    """
    from ..services.firebase import admin_firestore

    def _fetch() -> bool:
        try:
            snap = admin_firestore().collection("users").document(uid).get()
            if not snap.exists:
                return False
            return (snap.to_dict() or {}).get("aura_consent_granted", None) is False
        except Exception:
            return False

    return await asyncio.to_thread(_fetch)


async def _fetch_cached_aura_data(
    uid: str,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    now = datetime.now(UTC)

    # GDPR withdrawal: if the user has explicitly turned Aura memory off, do not
    # read or inject their stored profile (the writer is already consent-gated in
    # user_aura_extractor). This makes the in-app revoke stop personalization
    # within a turn instead of letting the frozen profile keep shaping chat.
    if await _aura_consent_revoked(uid):
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

# Kept in sync with lib/data/models/attachment_validator.dart
_SUPPORTED_IMAGE_MIME_TYPES: frozenset[str] = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp",
})
_SUPPORTED_DOCUMENT_MIME_TYPES: frozenset[str] = frozenset({
    "application/pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/msword",
    "text/plain", "text/csv", "text/tab-separated-values", "text/html", "application/rtf",
    "application/epub+zip",
})
_MAX_ATTACHMENTS_PER_REQUEST = 5
_MAX_IMAGE_BASE64_SIZE = 7_000_000      # ~5 MB raw * 1.33 base64 overhead
_MAX_DOCUMENT_BASE64_SIZE = 14_000_000  # ~10 MB raw * 1.33 base64 overhead


class AttachmentRejection:
    """Details about a rejected attachment for the 422 response."""

    __slots__ = ("index", "file_name", "reason")

    def __init__(self, index: int, file_name: str, reason: str) -> None:
        self.index = index
        self.file_name = file_name
        self.reason = reason

    def to_dict(self) -> dict[str, Any]:
        return {"index": self.index, "file_name": self.file_name, "reason": self.reason}


def _validate_and_filter_attachments(
    raw: list[Any],
    user_id: str,
) -> tuple[list[dict[str, Any]], list[AttachmentRejection]]:
    """
    Server-side trust boundary: validate attachment count, MIME type, and data size.
    Returns (accepted, rejections). Caller should 422 when rejections is non-empty.
    """
    if not raw or not isinstance(raw, list):
        return [], []

    accepted: list[dict[str, Any]] = []
    rejections: list[AttachmentRejection] = []

    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        file_name = str(item.get("file_name", f"attachment_{i}"))

        if len(accepted) >= _MAX_ATTACHMENTS_PER_REQUEST:
            rejections.append(AttachmentRejection(i, file_name, "max 5 attachments per message"))
            continue

        mime = item.get("mime_type", "")
        att_type = item.get("type", "")
        data = item.get("data", "")

        if not isinstance(data, str) or not data:
            rejections.append(AttachmentRejection(i, file_name, "missing or empty data"))
            continue

        if att_type == "image" and mime in _SUPPORTED_IMAGE_MIME_TYPES:
            if len(data) > _MAX_IMAGE_BASE64_SIZE:
                rejections.append(AttachmentRejection(i, file_name, "image exceeds 5 MB"))
                continue
            accepted.append(item)
        elif att_type == "document" and mime in _SUPPORTED_DOCUMENT_MIME_TYPES:
            if len(data) > _MAX_DOCUMENT_BASE64_SIZE:
                rejections.append(AttachmentRejection(i, file_name, "document exceeds 10 MB"))
                continue
            accepted.append(item)
        else:
            rejections.append(AttachmentRejection(i, file_name, f"unsupported type: {mime}"))

    if rejections:
        logger.warn("Chat: attachments rejected", {
            "user_id": user_id,
            "rejected": [r.to_dict() for r in rejections],
        })

    return accepted, rejections


def _build_user_content(
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


def _build_system_blocks(
    base_system_prompt: str,
    aura_suffix: str,
    local_datetime: str,
) -> list[dict[str, Any]]:
    """
    Build the Anthropic system parameter as a list of TextBlockParams with
    prompt-cache breakpoints.

    Layout (stable → volatile, so the cache prefix is as long as possible):
      Block 1: base prompt                          [cache_control]  — never changes
      Block 2: aura suffix                          [cache_control]  — stable for ~10 min
      Block 3: current datetime                                      — not cached

    Anthropic evaluates cache breakpoints in tools → system → messages order.
    The list format is required for explicit cache_control placement; a plain
    string only supports automatic (top-level) caching which cannot exclude the
    volatile datetime from the cached prefix.
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
    return blocks


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
        logger.warn("Chat: rejected, missing user_id")
        return _sse_error_response(
            "Unauthorized: user_id required",
            status_code=401,
            headers=_sse_headers,
        )

    # effective_tier is always resolved so it can be passed to the
    # Claude client for tool-level gating regardless of environment.
    effective_tier = "pro"
    if settings.is_production:
        from ..services.entitlement import (
            check_and_increment_daily_chat_usage,
            get_user_effective_tier,
        )
        effective_tier = await get_user_effective_tier(user_id)
        if effective_tier == "free":
            allowed, _ = await check_and_increment_daily_chat_usage(user_id)
            if not allowed:
                logger.info("Chat: free-tier daily limit reached", {"user_id": user_id})
                return StreamingResponse(
                    _chat_limit_reached_stream(),
                    media_type="text/event-stream",
                    status_code=200,
                    headers=_sse_headers,
                )

    message = str(body.get("message", "")).strip()
    raw_attachments: list[Any] = body.get("attachments") or []
    if not message and not raw_attachments:
        logger.warn("Chat: rejected, empty message", {"user_id": user_id})
        return _sse_error_response("message or attachments required", status_code=400, headers=_sse_headers)
    if len(message) > 8_000:
        logger.warn(
            "Chat: rejected, message too long",
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
    history: list[dict[str, Any]] = []
    for h in raw_history:
        if not isinstance(h, dict) or h.get("role") not in ("user", "assistant") or not h.get("content"):
            continue
        if len(history) >= settings.CHAT_HISTORY_WINDOW:
            break
        content = h["content"]
        if isinstance(content, list):
            history.append({"role": str(h["role"]), "content": content})
        else:
            history.append({"role": str(h["role"]), "content": str(content)})

    client_message_id: str | None = body.get("client_message_id") or None
    validated_attachments, attachment_rejections = _validate_and_filter_attachments(raw_attachments, user_id)

    if attachment_rejections:
        return _sse_error_response(
            f"Invalid attachments: {', '.join(r.file_name + ' (' + r.reason + ')' for r in attachment_rejections)}",
            status_code=422,
            headers=_sse_headers,
        )

    prev_buddy_response: str | None = next(
        (h["content"] for h in reversed(history) if h["role"] == "assistant"),
        None,
    )

    # Build system prompt: fetch datetime + aura profile concurrently, logging query off the critical path.
    (local_datetime, (aura_profile, accepted_hints)) = await asyncio.gather(
        _get_user_local_datetime(user_id),
        _fetch_cached_aura_data(user_id),
    )
    aura_suffix = _build_injected_system_prompt_suffix(aura_profile, accepted_hints, user_id)
    system_prompt_blocks = _build_system_blocks(
        settings.BUDDY_CHAT_SYSTEM_PROMPT,
        aura_suffix,
        local_datetime,
    )

    asyncio.create_task(
        log_query(
            user_id,
            "chat",
            message,
            session_id=session_id,
            client_message_id=client_message_id,
        )
    )
    asyncio.create_task(
        extract_and_update_user_aura(user_id, message, session_id, prev_buddy_response)
    )

    user_content = _build_user_content(message, validated_attachments)

    logger.info(
        "Chat: stream request received",
        {
            "user_id": user_id,
            "session_id": session_id,
            "message_len": len(message),
            "history_turns": len(history),
            "attachment_count": len(validated_attachments),
        },
    )

    start_ts = time.monotonic()

    async def _generate() -> AsyncGenerator[str, None]:
        try:
            tool_executor = ToolExecutor(user_id, created_via="text")
            claude = ClaudeClient(tool_executor)
            async for sse_event in claude.send_text_turn_stream(
                system_prompt=system_prompt_blocks,
                user_content=user_content,
                history=history,
                is_agent=False,
                user_tier=effective_tier,
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
            _err = json.dumps({
                "type": "error",
                "message": "Something glitched on my end there. Mind sending that again?",
            })
            yield f"data: {_err}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_sse_headers)
