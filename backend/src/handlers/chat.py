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
from typing import Any

from fastapi.responses import StreamingResponse

from ..config.settings import settings
from ..lib.logger import logger
from ..lib.query_logger import log_query
from ..services.chat_error_copy import CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE
from ..services.claude_client import ClaudeClient
from ..services.request_auth import resolve_user_id
from ..services.tool_executor import ToolExecutor
from ..services.user_aura_extractor import extract_and_update_user_aura
from ..services.chat_completion import prompt_builder as _prompt_builder
from ..services.chat_completion import turn_store
from ..services.chat_completion.prompt_builder import build_turn_system_blocks, fetch_user_doc
from ..services.engagement.task_scheduler import get_task_scheduler

_build_user_content = _prompt_builder.build_user_content
_NOTIFICATION_REASON_MAX_CHARS = _prompt_builder.NOTIFICATION_REASON_MAX_CHARS
_build_system_blocks = _prompt_builder.build_system_blocks
_build_injected_system_prompt_suffix = _prompt_builder.build_injected_system_prompt_suffix


async def _reconcile_and_schedule_intents(
    user_id: str,
    message: str,
    prev_buddy_response: str | None,
    user_doc: dict[str, Any] | None = None,
) -> None:
    """Lazy-imported wrapper for the reactive intent sensor, so chat.py stays
    decoupled from the reactive package at module load. Never raises."""
    try:
        from ..services.reactive.intent_sense import reconcile_and_schedule

        await reconcile_and_schedule(user_id, message, prev_buddy_response, user_doc=user_doc)
    except Exception as exc:
        logger.warn("chat: intent sense task failed (swallowed)", {
            "user_id": user_id, "error": str(exc),
        })


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


_NOTIFICATION_REASON_MAX_CHARS = 600


def _build_system_blocks(
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
            EntitlementUnavailableError,
            check_and_increment_daily_chat_usage,
            get_user_effective_tier,
        )
        try:
            effective_tier = await get_user_effective_tier(user_id)
        except EntitlementUnavailableError:
            # Never hand out pro on an outage. "free" here only tightens tool
            # gating for the turn; the usage counter below fails open on the
            # same outage, so the user is degraded, never hard-blocked.
            effective_tier = "free"
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

    # Sent ONLY on the first chat turn after a proactive-notification tap (the client
    # drops it after one send). It is the Buddy-facing "why I reached out" note from
    # the push payload; injected into the system prompt below so Buddy stays oriented
    # on the opener it sent. Capped defensively (the producers already keep it short).
    notification_reason = str(body.get("notification_reason") or "").strip()[
        :_NOTIFICATION_REASON_MAX_CHARS
    ]

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

    # Single read of users/{uid} for this turn, shared below instead of 4
    # independent re-fetches (datetime, aura-revoke check, and the two fire-and-
    # forget tasks' own consent checks) -- see firestore_read_audit_20260706 memory.
    user_doc = await fetch_user_doc(user_id)

    # Build the full system prompt (datetime + aura profile suffix + query-relevant
    # long-term memory) via the shared assembler, so the live turn here and the durable
    # background completion (services/chat_completion) construct the EXACT same prompt.
    system_prompt_blocks = await build_turn_system_blocks(
        user_id, message, notification_reason, user_doc=user_doc,
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
        extract_and_update_user_aura(
            user_id, message, session_id, prev_buddy_response, user_doc=user_doc,
        )
    )
    # Reactive layer: detect resolutions ("mom is fine" -> cancel the queued surgery
    # follow-up) and future concerns ("mom has surgery tomorrow" -> schedule one).
    # Fire-and-forget, consent-gated + cost-capped, never touches the stream.
    asyncio.create_task(
        _reconcile_and_schedule_intents(user_id, message, prev_buddy_response, user_doc)
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

    # Durable background completion: record this turn and enqueue a delayed Cloud Task so
    # that if the phone disconnects mid-stream (the generator below is cancelled and the
    # answer is lost), the turn still finishes server-side and pushes the reply. 
    if client_message_id:
        await turn_store.start_turn(
            user_id,
            client_message_id,
            session_id=session_id,
            message=message,
            history=history,
            has_attachments=bool(validated_attachments),
            tier=effective_tier,
            notification_reason=notification_reason,
        )
        try:
            await asyncio.to_thread(
                get_task_scheduler().schedule_chat_completion,
                user_id,
                client_message_id,
                session_id or "",
                settings.CHAT_COMPLETION_DELAY_SECONDS,
            )
        except Exception as exc:
            logger.warn("Chat: completion task enqueue failed (backstop sweep covers it)", {
                "user_id": user_id, "cmid": client_message_id, "error": str(exc),
            })

    async def _generate() -> AsyncGenerator[str, None]:
        try:
            tool_executor = ToolExecutor(
                user_id, created_via="text", client_message_id=client_message_id or "",
            )
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
            # The full stream was delivered to the client: mark the turn done so the
            # pending completion task becomes a no-op. Reached only when the loop finishes
            # without the client disconnecting (a disconnect cancels the generator before
            # here, leaving the turn 'generating' for the task to finish + push).
            if client_message_id:
                await turn_store.mark_client_complete(user_id, client_message_id)
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
                "message": CHAT_TEMPORARILY_UNAVAILABLE_MESSAGE,
            })
            yield f"data: {_err}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(_generate(), media_type="text/event-stream", headers=_sse_headers)
